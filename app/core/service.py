# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""mijiaAPI 适配层——全项目唯一允许 import mijiaAPI 的模块。

上游库只从 PyPI 安装升级（版本锁在 5.x，见 pyproject.toml），接口一旦变化
只需要修改这里；界面层与线程层完全不感知第三方类型的存亡。

5.0.0 的破坏性变更：设备规格数据源从 home.miot-spec.com 迁到
miot-spec.org（小米官方数据源），get_device_info() 的首个参数由设备型号
model 改为 spec_type（规格类型 URN，随 get_devices_list() 的条目一起
下发）。本项目因此把 did -> (model, name) 索引扩成 (model, name,
spec_type)，spec 内存缓存也改以 spec_type 为键——与上游
{spec_type}.json 缓存文件一一对应，同规格的多台设备只拉一次。

依赖的上游半公开方法说明：扫码登录拆成 _get_qr_login_data 与
_complete_qr_login 两步使用，是因为上游的 login() 会把二维码打印到
终端，图形界面拿不到；官方 MCP server 也采用同样的两步组合。
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple

from mijiaAPI import (
    DeviceNotFoundError,
    GetDeviceInfoError,
    get_device_info,
    mijiaAPI,
    mijiaDevice,
)
from mijiaAPI.devices import DevAction, DevProp
from mijiaAPI.miutils import generate_enc_params, gen_nonce, get_signed_nonce

from . import icon_store
from .models import ActionInfo, DeviceDetail, DeviceInfo, PropInfo

logger = logging.getLogger(__name__)

# 单次批量属性读取的设备数上限，防止请求体过大被网关拒绝
_BATCH_SIZE = 15
# spec 并发拉取线程数；目标是 miot-spec.org 的独立请求，无会话竞争
_SPEC_WORKERS = 8
# 无公开功能规格的统一提示：可能是云端没下发 spec_type，也可能是
# miot-spec.org 上没有该规格
_NO_SPEC_HINT = "该设备无公开的功能规格（常见于仅蓝牙连接的产品），无法提供控制面板"


class _DeviceRef(NamedTuple):
    """设备列表索引条目。

    spec_type 是 5.0.0 起拉取功能规格的唯一入参（规格类型 URN）；云端
    未下发该字段的设备留空字符串，这种设备拿不到任何公开规格。
    """

    model: str = ""
    name: str = ""
    spec_type: str = ""


class ServiceError(Exception):
    """携带用户可直接阅读的中文信息，界面层直接展示 message 即可。"""


def _wrap_error(exc: Exception, context: str) -> ServiceError:
    # 上游异常的 args 结构各不相同，原样拼接足够定位问题又不至于暴露噪音；
    # 调用方统一用 raise ... from exc 保持原始堆栈链
    detail = "; ".join(str(a) for a in exc.args)
    return ServiceError(f"{context}: {detail}")


class MijiaService:
    def __init__(self):
        # 认证文件沿用上游默认位置 ~/.config/mijia-api/auth.json，
        # 这样 CLI 里扫过的码在 GUI 直接生效，反之亦然
        self._api = self._init_api()
        self._device_cache: dict[str, mijiaDevice] = {}
        # did -> 型号/名称/规格类型索引，批量读状态与共享设备组装时
        # 避免反复拉设备列表；自有与共享设备都在其中
        self._device_index: dict[str, _DeviceRef] = {}
        # spec_type -> spec 内存缓存，键与上游 {spec_type}.json 缓存文件
        # 一一对应：同规格的多台设备只拉一次，也不会并发写坏同一份文件
        self._spec_cache: dict[str, dict | None] = {}
        # model -> spec_type，界面层按型号查「有无公开功能」时反查用
        self._model_spec_types: dict[str, str] = {}
        # spec 产品名缓存就绪后的产品页中文名缓存：
        # model -> 中文名或 None（已确认无中文名，不再重查）
        self._product_page_names: dict[str, str | None] = {}
        # model -> 图标 URL 或 None（拉取失败，会话内不再重试）；
        # 启动即从磁盘加载，此后只增不删（见 icon_store）
        self._icon_cache: dict[str, str | None] = icon_store.load_urls()

    def _init_api(self) -> mijiaAPI:
        """构造上游客户端；认证文件损坏时隔离坏文件并降级为未登录。

        上游构造函数同步读取并解析 auth.json 且写入非原子，进程被杀
        可能留下半截文件；不在这里接住，异常会穿透到 QApplication
        之前，用户只会看到进程无声退出。
        """
        try:
            return mijiaAPI()
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
            auth_path = Path.home() / ".config" / "mijia-api" / "auth.json"
            try:
                auth_path.replace(auth_path.with_name("auth.json.corrupt"))
            except OSError:
                pass
            logger.warning("认证文件损坏已隔离为 auth.json.corrupt，请重新扫码登录: %s", exc)
            # 文件已移除，上游按「无认证文件」处理，available 恒为 False
            return mijiaAPI()

    # ---------- 登录 ----------

    def login_status(self) -> bool:
        try:
            return self._api.available
        except Exception:
            return False

    def qr_login_begin(self) -> dict | None:
        """获取扫码登录数据。

        返回 None 表示本地凭据已自动刷新、无需扫码；
        否则返回含 loginUrl 的 dict，交由界面渲染二维码。
        """
        try:
            data = self._api._get_qr_login_data()
        except Exception as exc:
            # 不只捕 LoginError：断网时 requests 连接异常也要转成
            # 界面可直接展示的中文信息
            raise _wrap_error(exc, "获取登录二维码失败") from exc
        if data.get("refreshed"):
            return None
        return data

    def qr_login_wait(self, login_data: dict) -> None:
        """长轮询等待用户扫码，阻塞可达两分钟，必须放在后台线程调用。"""
        try:
            self._api._complete_qr_login(login_data)
        except Exception as exc:
            raise _wrap_error(exc, "扫码登录未完成") from exc

    # ---------- 设备列表 ----------

    def list_devices(self) -> list[DeviceInfo]:
        """拉取全部家庭和共享设备并补齐房间归属。

        设备信息本身不含房间字段，参照 CLI 的实现思路：
        遍历每个家庭的 roomlist[].dids[] 反查出 did -> (家庭, 房间)。
        """
        try:
            homes = self._api.get_homes_list()
            devices = self._api.get_devices_list() + self._api.get_shared_devices_list()
        except Exception as exc:
            raise _wrap_error(exc, "获取设备列表失败") from exc

        location: dict[str, tuple[str, str]] = {}
        for home in homes:
            for room in home.get("roomlist", []):
                for did in room.get("dids", []) or []:
                    location[str(did)] = (home["name"], room["name"])

        result = []
        for d in devices:
            did = str(d["did"])
            home_name, room_name = location.get(did, ("未知", "未知"))
            result.append(DeviceInfo(
                did=did,
                name=d.get("name", did),
                model=d.get("model", ""),
                home_name=home_name,
                room_name=room_name,
                online=bool(d.get("isOnline", False)),
            ))
        return sorted(result, key=lambda x: (x.home_name, x.room_name, x.name))

    # ---------- 设备控制 ----------

    def device_detail(self, did: str) -> DeviceDetail:
        dev = self._get_device(did)
        # prop_list 对属性名里的 '-' 额外注册了一份 '_' 别名键，
        # 两者指向同一对象，按对象身份去重避免面板出现重复控件
        seen: set[int] = set()
        props: list[PropInfo] = []
        for prop in dev.prop_list.values():
            if id(prop) in seen:
                continue
            seen.add(id(prop))
            props.append(PropInfo(
                name=prop.name,
                desc=prop.desc or prop.name,
                type=prop.type,
                readable="r" in prop.rw,
                writable="w" in prop.rw,
                range=tuple(prop.range) if prop.range else None,
                value_list=prop.value_list,
            ))
        actions = [
            ActionInfo(name=a.name, desc=a.desc or a.name)
            for a in dev.action_list.values()
        ]
        return DeviceDetail(did=dev.did, name=dev.name, model=dev.model,
                            props=props, actions=actions)

    def read_prop(self, did: str, name: str):
        dev = self._get_device(did)
        try:
            return dev.get(name)
        except Exception as exc:
            raise _wrap_error(exc, f"读取属性 {name} 失败") from exc

    def read_props(self, did: str, names: list[str]) -> dict[str, Any | None]:
        """批量读取同一台设备的多个属性，一次请求替代逐个轮询。

        逐个读取时上游每个属性固定 sleep 0.5 秒，详情面板十几项
        属性要等近十秒；合并为批量请求后整面板一次往返即可完成。
        """
        dev = self._get_device(did)
        queries: list[dict] = []
        key_to_name: dict[tuple, str] = {}
        for name in names:
            prop = dev.prop_list.get(name)
            if prop is None or "r" not in prop.rw:
                continue
            method = prop.method.copy()
            method["did"] = dev.did
            queries.append(method)
            key_to_name[(method["siid"], method["piid"])] = name

        result: dict[str, Any | None] = {name: None for name in key_to_name.values()}
        for start in range(0, len(queries), _BATCH_SIZE):
            batch = queries[start:start + _BATCH_SIZE]
            try:
                rets = self._api.get_devices_prop(batch)
            except Exception as exc:
                raise _wrap_error(exc, "批量读取属性失败") from exc
            for item in rets:
                key = (item.get("siid"), item.get("piid"))
                name = key_to_name.get(key)
                if name is None:
                    continue
                result[name] = item["value"] if item.get("code") == 0 else None
        return result

    def write_prop(self, did: str, name: str, value) -> None:
        dev = self._get_device(did)
        try:
            dev.set(name, value)
        except Exception as exc:
            raise _wrap_error(exc, f"设置属性 {name} 失败") from exc

    def run_action(self, did: str, name: str, params=None) -> None:
        dev = self._get_device(did)
        try:
            if params is not None:
                # 小爱类文本指令需走 _in 通道（见上游 CLI 的
                # wifispeaker.run_action('execute-text-directive', _in=[prompt, quiet])）
                if name in ("execute-text-directive", "play-text", "play-music", "play-radio"):
                    # 统一按 _in 传递，兼容单字符串与列表
                    in_val = params if isinstance(params, (list, tuple)) else [params]
                    # execute-text-directive 为 [文本, 是否静默](0/1)，缺省按非静默补齐
                    if name == "execute-text-directive" and len(in_val) == 1:
                        in_val = [in_val[0], 0]
                    dev.run_action(name, _in=in_val)
                elif isinstance(params, (list, tuple)):
                    dev.run_action(name, params)
                else:
                    dev.run_action(name, [params])
            else:
                # 文本类动作无参必报 -704220025，这里直接引导上层弹输入
                if name in ("execute-text-directive", "play-text"):
                    raise ServiceError(f"动作 {name} 需要文本参数")
                dev.run_action(name)
        except ServiceError:
            raise
        except Exception as exc:
            raise _wrap_error(exc, f"执行动作 {name} 失败") from exc

    def _get_device(self, did: str) -> mijiaDevice:
        # 构造 mijiaDevice 本身要发两次网络请求（设备列表 + spec 拉取），
        # 按 did 缓存实例，面板切换时才不会反复打接口
        if did not in self._device_cache:
            try:
                self._device_cache[did] = mijiaDevice(self._api, did=did)
            except DeviceNotFoundError:
                # 上游构造只查自有设备列表（不含共享设备），共享设备
                # 必然在此失败；按上游 __init__ 的字段手工组装，后续
                # get/set/run_action 与自有设备走完全相同的代码路径
                self._device_cache[did] = self._build_shared_device(did)
            except GetDeviceInfoError as exc:
                # 无公开功能规格的设备（常见于仅蓝牙连接的产品）：
                # 没有属性/动作可控制，给出用户可读的明确提示
                raise ServiceError(_NO_SPEC_HINT) from exc
            except ValueError as exc:
                # 5.0.0 起上游用云端下发的 spec_type 拉规格，该字段缺失时
                # get_device_info 抛 ValueError 而非 GetDeviceInfoError；
                # 只有确实查不到 spec_type 才归为「无功能规格」，否则
                # （如属性类型不受上游支持）保留原始错误信息
                if not self._device_ref(did).spec_type:
                    raise ServiceError(_NO_SPEC_HINT) from exc
                raise _wrap_error(exc, "加载设备信息失败") from exc
            except Exception as exc:
                raise _wrap_error(exc, "加载设备信息失败") from exc
        return self._device_cache[did]

    def _device_ref(self, did: str) -> _DeviceRef:
        """did -> 索引条目；索引里没有时先刷新一次设备列表再取。

        取不到返回空条目（型号为空），调用方据此判定设备不存在。
        """
        if did not in self._device_index:
            self._refresh_device_index()
        return self._device_index.get(did, _DeviceRef())

    def _build_shared_device(self, did: str) -> mijiaDevice:
        """为共享设备手工组装 mijiaDevice（上游构造器只查自有设备列表）。"""
        ref = self._device_ref(did)
        if not ref.model:
            raise ServiceError(f"未找到设备 {did}")
        if not ref.spec_type:
            raise ServiceError(_NO_SPEC_HINT)
        try:
            dev_info = get_device_info(
                ref.spec_type, cache_path=self._api.auth_data_path.parent)
        except GetDeviceInfoError as exc:
            raise ServiceError(_NO_SPEC_HINT) from exc
        except Exception as exc:
            raise _wrap_error(exc, "加载设备信息失败") from exc

        dev = mijiaDevice.__new__(mijiaDevice)
        dev.api = self._api
        dev.did = did
        dev.model = ref.model
        dev.name = ref.name or dev_info.get("name", did)
        dev.sleep_time = 0.5
        # prop_list/action_list 必须最后赋值：上游 __setattr__ 在
        # prop_list 存在后会拦截同名属性写入并转发为设备控制
        prop_list: dict[str, DevProp] = {}
        for prop in dev_info.get("properties", []):
            prop_obj = DevProp(prop)
            prop_list[prop["name"]] = prop_obj
            if "-" in prop["name"]:
                prop_list[prop["name"].replace("-", "_")] = prop_obj
        dev.prop_list = prop_list
        dev.action_list = {
            act["name"]: DevAction(act) for act in dev_info.get("actions", [])
        }
        return dev

    # ---------- 开关状态（卡片快速控制用） ----------

    def power_state(self, did: str) -> bool | None:
        """读取单台设备开关状态；无可写开关属性的设备返回 None。

        走 power_states 的 spec 批量路径：model/spec 全部命中缓存时
        只剩一次属性请求，避免为单个开关构造 mijiaDevice（两次请求）。
        """
        return self.power_states([did]).get(did)

    def toggle_power(self, did: str) -> bool:
        """读取当前开关并取反写入，返回新状态。

        读与写必须串在同一任务里完成（调用方经单线程队列提交），
        否则两次轮询之间会出现读后写的竞态。
        """
        current = self.power_state(did)
        if current is None:
            raise ServiceError("设备不支持开关控制或已离线")
        new_state = not current
        self.set_power_state(did, new_state)
        return new_state

    def set_power_state(self, did: str, state: bool) -> None:
        """写入开关状态：spec method 直接构造批量写请求。

        不经 mijiaDevice.set——那条路要构造设备对象并逐属性写入，
        首次点击要等两三次网络往返；直写批量接口一次请求完成。
        """
        info = self._specs_for([did]).get(did)
        method = self._find_on_method(info) if info else None
        if method is None:
            raise ServiceError("设备不支持开关控制")
        try:
            rets = self._api.set_devices_prop(
                [{"did": did, **method, "value": state}]
            )
        except Exception as exc:
            raise _wrap_error(exc, "设置开关状态失败") from exc
        # 写接口的 code 语义与读不同：上游把 code∈(0,1) 的响应统一
        # 改写为「成功」（apis.py set_devices_prop 出口），其余一律失败
        items = rets if isinstance(rets, list) else [rets]
        for item in items:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            if code in (0, 1):
                continue
            raise ServiceError(
                f"设置失败（{item.get('message', '未知错误')}，错误码 {code}）"
            )

    def power_states(self, dids: list[str]) -> dict[str, bool | None]:
        """批量读取多台设备的开关状态。

        三段式压缩网络开销：did->spec_type 映射增量补齐（一次设备列表）、
        spec 线程池并发拉取（本地文件缓存后零开销）、属性按批合并
        读取。全部命中缓存时整批设备只剩一两次属性请求。
        """
        spec_map = self._specs_for(dids)

        queries: list[dict] = []
        result: dict[str, bool | None] = {}
        for did in dids:
            info = spec_map.get(did)
            method = self._find_on_method(info) if info else None
            if method is None:
                # spec 缺失或无开关属性，直接记为无能力
                result[did] = None
            else:
                queries.append({"did": did, **method})

        for start in range(0, len(queries), _BATCH_SIZE):
            batch = queries[start:start + _BATCH_SIZE]
            try:
                rets = self._api.get_devices_prop(batch)
            except Exception as exc:
                raise _wrap_error(exc, "批量读取设备状态失败") from exc
            for item in rets:
                did = str(item.get("did"))
                # code 非 0 多为设备离线，视为状态未知而非无能力；
                # 这里同样记 None：按钮隐藏，下轮轮询会再尝试
                result[did] = bool(item["value"]) if item.get("code") == 0 else None
        return result

    def _fetch_spec(self, spec_type: str, cache_dir) -> dict | None:
        if not spec_type:
            return None
        try:
            return get_device_info(spec_type, cache_path=cache_dir)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 上游缓存文件损坏（多见于并发写坏）。不删掉重拉的话，
            # 该规格的能力信息将永久缺失，开关与温湿度控件随之消失
            try:
                self._spec_cache_file(cache_dir, spec_type).unlink(missing_ok=True)
            except OSError:
                return None
            try:
                return get_device_info(spec_type, cache_path=cache_dir)
            except Exception:
                return None
        except Exception:
            # 个别规格在 miot-spec.org 上不存在（如第三方牙刷），按无能力处理
            return None

    @staticmethod
    def _spec_cache_file(cache_dir, spec_type: str) -> Path:
        """上游 5.0.0 的规格缓存文件路径：{spec_type}.json，其中 ':' 换成 '_'。"""
        return Path(cache_dir) / f"{spec_type.replace(':', '_')}.json"

    @staticmethod
    def _find_on_method(info: dict) -> dict | None:
        for prop in info.get("properties", []):
            if prop.get("type") == "bool" and "w" in prop.get("rw", "") and (
                prop["name"] == "on"
                or prop["name"].replace("_", "-").startswith("on-")
            ):
                return prop["method"]
        return None

    # ---------- 环境读数（卡片副标题展示用） ----------

    def read_metrics(self, dids: list[str]) -> dict[str, str | None]:
        """批量读取温湿度并拼成卡片副标题文案。

        匹配 SI 标准属性：temperature 与 relative-humidity（个别型号
        也叫 humidity），读不到的设备返回 None（副标题维持纯房间名）。
        多通道设备的同名属性取第一个通道。
        """
        spec_map = self._specs_for(dids)
        queries: list[dict] = []
        key_to_metric: dict[tuple, tuple[str, str]] = {}  # (did,siid,piid) -> (did, metric)
        result: dict[str, str | None] = {}
        for did in dids:
            info = spec_map.get(did)
            found: dict[str, dict] = {}
            if info:
                for prop in info.get("properties", []):
                    name = prop.get("name")
                    metric = (
                        "temperature" if name == "temperature"
                        else "humidity" if name in ("relative-humidity", "humidity")
                        else None
                    )
                    if metric and "r" in prop.get("rw", "") and metric not in found:
                        found[metric] = prop["method"]
            if not found:
                result[did] = None
                continue
            for metric, method in found.items():
                queries.append({"did": did, **method})
                key_to_metric[(did, method["siid"], method["piid"])] = (did, metric)

        temps: dict[str, float] = {}
        hums: dict[str, float] = {}
        for start in range(0, len(queries), _BATCH_SIZE):
            batch = queries[start:start + _BATCH_SIZE]
            try:
                rets = self._api.get_devices_prop(batch)
            except Exception:
                # 读数失败只影响副标题展示，不值得打断主流程
                return {did: None for did in dids}
            for item in rets:
                entry = key_to_metric.get(
                    (str(item.get("did")), item.get("siid"), item.get("piid"))
                )
                if entry is None or item.get("code") != 0:
                    continue
                did, metric = entry
                try:
                    value = float(item["value"])
                except (KeyError, TypeError, ValueError):
                    continue
                if metric == "temperature":
                    temps[did] = value
                else:
                    hums[did] = value

        for did in dids:
            result[did] = format_metrics_text(temps.get(did), hums.get(did))
        return result

    def _specs_for(self, dids: list[str]) -> dict[str, dict | None]:
        """did -> spec 映射，spec_type 增量补齐 + spec 并发拉取。

        与 power_states 共用设备索引、内存 spec 缓存与上游文件缓存，
        轮询同时刷开关和读数时不会产生额外请求。同一 spec_type 只拉
        一次：上游把 spec 缓存在按 spec_type 命名的同一个文件里，
        并发拉取同一份规格会互相覆盖写坏缓存（同型号、以及不同型号
        但共用规格的设备都算在内）。
        """
        if any(d not in self._device_index for d in dids):
            self._refresh_device_index()

        result: dict[str, dict | None] = {}
        to_fetch: list[str] = []
        for did in dids:
            spec_type = self._device_index.get(did, _DeviceRef()).spec_type
            if not spec_type:
                # 云端没下发 spec_type，或设备不在列表里 → 视为无能力
                result[did] = None
            elif spec_type in self._spec_cache:
                result[did] = self._spec_cache[spec_type]
            elif spec_type not in to_fetch:
                to_fetch.append(spec_type)

        if to_fetch:
            cache_dir = self._api.auth_data_path.parent
            with ThreadPoolExecutor(max_workers=min(_SPEC_WORKERS, len(to_fetch))) as pool:
                fetched = pool.map(
                    lambda st: self._fetch_spec(st, cache_dir), to_fetch
                )
                for spec_type, spec in zip(to_fetch, fetched):
                    self._spec_cache[spec_type] = spec
            for did in dids:
                if did not in result:
                    result[did] = self._spec_cache.get(
                        self._device_index.get(did, _DeviceRef()).spec_type
                    )
        return result

    def _refresh_device_index(self) -> None:
        """重建 did -> (model, name, spec_type) 索引，自有与共享设备合并。"""
        try:
            all_devices = (
                self._api.get_devices_list()
                + self._api.get_shared_devices_list()
            )
        except Exception as exc:
            raise _wrap_error(exc, "获取设备列表失败") from exc
        for d in all_devices:
            did = str(d["did"])
            model = d.get("model", "")
            # 5.0.0 起规格按 spec_type 拉取；云端偶尔不下发该字段，
            # 归一成空字符串，调用方据此判定「无公开功能规格」
            spec_type = d.get("spec_type") or ""
            self._device_index[did] = _DeviceRef(model, d.get("name", ""), spec_type)
            # 同一型号的多台设备共用一条 model -> spec_type 记录；
            # 优先保留已拿到的非空值（个别接口不下发 spec_type）
            if model and not self._model_spec_types.get(model):
                self._model_spec_types[model] = spec_type

    # ---------- 设备图标 ----------

    def icon_urls(self, models: list[str]) -> dict[str, str | None]:
        """model -> 图标 URL，增量拉取：只对缓存里没有的型号调接口。

        成功结果写入内存与磁盘缓存；失败仅记内存 None（本次会话不再
        重试，下次启动重新尝试），避免网络抖动时每轮刷新重复打接口。
        """
        if not models:
            return {}
        missing = [m for m in models if m not in self._icon_cache]
        if missing:
            for model in missing:
                self._icon_cache[model] = self._fetch_icon_url(model)
            icon_store.save_urls(
                {k: v for k, v in self._icon_cache.items() if v})
        return {m: self._icon_cache[m] for m in models if self._icon_cache.get(m)}

    def _fetch_icon_url(self, model: str) -> str | None:
        """调 productconfig/get_icon 取图标 CDN 地址（302 重定向的 Location）。

        与 _request 的差异：这个接口靠重定向返回图片地址而非 JSON 响应体，
        必须关闭自动跟随重定向自己读 Location；签名与刷新逻辑保持一致。
        """
        uri = "/v2/productconfig/get_icon"
        try:
            self._api._refresh_token()
            params = {"data": json.dumps(
                {"icon_name": "icon_real", "model": model},
                separators=(",", ":"))}
            nonce = gen_nonce()
            signed_nonce = get_signed_nonce(self._api.auth_data["ssecurity"], nonce)
            params = generate_enc_params(
                uri, "POST", signed_nonce, nonce, params,
                self._api.auth_data["ssecurity"])
            ret = self._api.session.post(
                self._api.api_base_url + uri, data=params,
                allow_redirects=False, timeout=30)
        except Exception as exc:
            logger.warning("获取设备图标失败 %s: %s", model, exc)
            return None
        if ret.status_code != 302:
            logger.info("获取设备图标未重定向 %s: HTTP %s", model, ret.status_code)
            return None
        return ret.headers.get("Location")

    def icon_file(self, model: str, url: str) -> Path | None:
        """下载图标到本地缓存并返回文件路径；已存在直接返回，失败返回 None。"""
        path = icon_store.icon_path(model)
        if path.exists():
            return path
        import requests

        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200 or not r.content:
                logger.warning("下载设备图标失败 %s: HTTP %s", model, r.status_code)
                return None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
            return path
        except Exception as exc:
            logger.warning("下载设备图标失败 %s: %s", model, exc)
            return None

    def has_product_page_name(self, model: str) -> bool:
        """该型号的产品页中文名是否已解析过（含“确认无”的情况）。"""
        return model in self._product_page_names

    def cached_product_page_names(self, models: list[str]) -> dict[str, str]:
        """已解析且含中文的产品页名称（model -> name）。"""
        result: dict[str, str] = {}
        for model in models:
            name = self._product_page_names.get(model)
            if name and any("\u4e00" <= ch <= "\u9fff" for ch in name):
                result[model] = name
        return result

    def product_page_name(self, model: str) -> str | None:
        """抓取 miot-spec 产品页（/p/<model>）的中文商品名。

        用于无公开 spec 的设备（如仅蓝牙类）：这类设备在 /spec/ 路径
        404、specSummary.available=False，没有 spec 产品名可读，只有
        产品页里有本地化商品名。结果（含 None=无中文名）写入缓存；
        网络异常向上抛出由调用方决定重试。阻塞，须后台线程调用。
        """
        if model in self._product_page_names:
            return self._product_page_names[model]
        import requests

        r = requests.get(f"https://home.miot-spec.com/p/{model}", timeout=30,
                         headers={"User-Agent": "Mozilla/5.0"})
        name = None
        if r.status_code == 200:
            m = re.search(
                r'<script data-page="app" type="application/json">(.*?)</script>',
                r.text, re.S)
            if m:
                product = json.loads(m.group(1)).get("props", {}).get("product", {})
                name = product.get("name") or None
                if name is not None and not any("一" <= ch <= "鿿" for ch in name):
                    name = None  # 产品名也非中文，无回退价值
        self._product_page_names[model] = name
        return name

    def model_has_published_functions(self, model: str) -> bool | None:
        """该型号是否发布过含属性的功能 spec。

        True=有属性；False=无 spec 或 spec 无属性（无可控制功能）；
        None=spec 尚未拉取（未知，调用方应视为有并等待轮询证实）。
        """
        if model not in self._model_spec_types:
            return None
        spec_type = self._model_spec_types[model]
        if not spec_type:
            # 云端未下发 spec_type：这类设备不存在可拉取的公开规格
            return False
        if spec_type not in self._spec_cache:
            return None
        spec = self._spec_cache[spec_type]
        return bool(spec and spec.get("properties"))

    def localized_product_names(self, dids: list[str], names: dict[str, str]) -> dict[str, str]:
        """did -> spec 中文产品名，用于替换未改名的英文默认设备名。

        米家 APP 对未改名设备显示的是产品库本地化商品名，而第三方
        设备列表接口的 name 字段只有英文默认名（国际版产品尤甚）。
        本方法从**已缓存的 spec**（不发起网络请求）读取：仅当云端名为
        纯 ASCII（未改名）且 spec 规格名含中文时返回，否则该 did 不在
        结果中（保持云端原名）。

        注意：5.0.0 起 spec 来自 miot-spec.org，其规格名是英文描述
        （如 "Outlet"、"Air Purifier"），中文名只存在于各服务的 comment
        字段里，取不到产品名——所以本方法通常不再产出结果，实际生效的
        是界面层抓取产品页（/p/<model>）那条兜底路径。这里保留，是为
        上游日后重新提供本地化规格名时零成本复用。
        """
        def has_cjk(s: str) -> bool:
            return any("\u4e00" <= ch <= "\u9fff" for ch in s)

        result: dict[str, str] = {}
        for did in dids:
            name = names.get(did, "")
            if not name or has_cjk(name):
                continue  # 已是中文（用户改名或国内默认名），不替换
            spec_type = self._device_index.get(did, _DeviceRef()).spec_type
            spec = self._spec_cache.get(spec_type) if spec_type else None
            if not spec:
                continue
            spec_name = str(spec.get("name") or "")
            if spec_name and has_cjk(spec_name) and spec_name != name:
                result[did] = spec_name
        return result


def format_metrics_text(temp, hum) -> str | None:
    """温湿度展示文案（如 "28.3°C 60%"）；两项都无效时返回 None。

    卡片副标题与详情面板回读共用，避免量纲启发式两处漂移。
    """
    parts: list[str] = []
    if temp is not None:
        try:
            parts.append(_format_temp(float(temp)))
        except (TypeError, ValueError):
            pass
    if hum is not None:
        try:
            parts.append(_format_humidity(float(hum)))
        except (TypeError, ValueError):
            pass
    return " ".join(parts) if parts else None


def _format_temp(value: float) -> str:
    # SI 规范里 temperature 常以 0.1 摄氏度步进的整数存储（283 = 28.3），
    # 按合理室温范围启发式区分真实值与 0.1 度整数值
    if abs(value) > 60:
        value /= 10
    return f"{value:.1f}°C"


def _format_humidity(value: float) -> str:
    # 湿度同样存在 0.1% 步进的整数存储（683 = 68.3%）
    if value > 100:
        value /= 10
    return f"{value:.0f}%"
