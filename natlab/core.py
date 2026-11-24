"""TCP NAT 演练台的核心状态机。

设计约束（与页面上的演练规则一一对应）：

* 公网端口按 (公网 IP, 公网端口, 上游端点) 这一组键复用：不同上游端点可以
  落在同一个公网端口上；同一出口、同一上游端点的多条连接必须拿到互不相同
  的公网端口。不同出口之间互不影响。
* 换址时旧出口进入“退场”：已建连接继续保留旧公网 IP 收包，关闭后仍在旧
  出口冷却；新连接只从当前出口分配端口。退场出口的连接全关、冷却全到期后
  自动移除。
* 连接关闭后，(公网 IP, 公网端口, 原上游端点) 进入固定长度的冷却期，冷却
  期内不能再分配给该上游；其他上游、其他出口不受影响。
* 所有时间都使用“演练时钟”（虚拟秒），不依赖真实时间，方便值班员推进和复现。
* 所有公开方法都在同一把可重入锁内完成校验和状态变更，Flask 多线程下也不会
  出现检查/写入竞态。并发建连与换址要么完整落在切换前，要么完整落在切换后。
"""

from __future__ import annotations

import ipaddress
import threading
from dataclasses import dataclass
from typing import Any

COOLDOWN_SECONDS = 60
MIN_PORT = 1
MAX_PORT = 65535


def format_elapsed(seconds: int) -> str:
    """把演练秒数格式化为 T+[Nd ]HH:MM:SS。"""

    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"T+{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"T+{hours:02d}:{minutes:02d}:{secs:02d}"


def format_host_port(ip: str, port: int) -> str:
    """IPv6 地址加方括号，避免 ip:port 产生歧义。"""

    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


class NatError(Exception):
    """可安全返回给页面的业务错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def _is_ascii_digits(text: str) -> bool:
    """只接受 ASCII 数字，避免全角数字等 Unicode 数字被 int() 悄悄接受。"""

    return bool(text) and all("0" <= char <= "9" for char in text)


def _parse_ip(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NatError("INVALID_INPUT", f"{field}必须是有效的 IP 地址", details={"field": field})
    try:
        # str() 会做压缩/规范化，后续匹配使用同一规范形式。
        return str(ipaddress.ip_address(value.strip()))
    except ValueError as exc:
        raise NatError(
            "INVALID_INPUT",
            f"{field}“{value.strip()}”不是有效的 IP 地址",
            details={"field": field},
        ) from exc


def _parse_optional_ip(value: Any, field: str) -> str | None:
    """回包目标 IP 可省略；空字符串与 None 都视为“使用当前出口”。"""

    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return _parse_ip(value, field)


def _parse_port(value: Any, field: str) -> int:
    port: int | None = None
    if isinstance(value, bool):
        port = None
    elif isinstance(value, int):
        port = value
    elif isinstance(value, float) and value.is_integer():
        port = int(value)
    elif isinstance(value, str) and _is_ascii_digits(value.strip()):
        port = int(value.strip())
    if port is None or not MIN_PORT <= port <= MAX_PORT:
        raise NatError(
            "INVALID_INPUT",
            f"{field}必须是 {MIN_PORT}-{MAX_PORT} 之间的整数",
            details={"field": field},
        )
    return port


def _parse_seconds(value: Any, field: str = "推进秒数") -> int:
    amount: int | None = None
    if isinstance(value, bool):
        amount = None
    elif isinstance(value, int):
        amount = value
    elif isinstance(value, float) and value.is_integer():
        amount = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if _is_ascii_digits(text):
            amount = int(text)
    if amount is None or amount < 0:
        raise NatError(
            "INVALID_INPUT",
            f"{field}必须是非负整数秒",
            details={"field": field},
        )
    return amount


@dataclass(frozen=True)
class Endpoint:
    ip: str
    port: int

    @property
    def label(self) -> str:
        return format_host_port(self.ip, self.port)

    def to_dict(self) -> dict[str, Any]:
        return {"ip": self.ip, "port": self.port, "label": self.label}


@dataclass(frozen=True)
class PublicConfig:
    public_ip: str
    port_min: int
    port_max: int

    @property
    def size(self) -> int:
        return self.port_max - self.port_min + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "public_ip": self.public_ip,
            "port_min": self.port_min,
            "port_max": self.port_max,
            "size": self.size,
            "label": f"{self.public_ip}:{self.port_min}-{self.port_max}",
        }


@dataclass(frozen=True)
class Connection:
    id: int
    internal: Endpoint
    upstream: Endpoint
    egress_ip: str
    public_port: int
    created_at: int


@dataclass(frozen=True)
class Cooldown:
    egress_ip: str
    public_port: int
    upstream: Endpoint
    internal: Endpoint
    connection_id: int
    closed_at: int
    until: int


class NatTable:
    """单个演练会话的 NAT 映射表。"""

    def __init__(self, *, cooldown_seconds: int = COOLDOWN_SECONDS, start_at: int = 0) -> None:
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds 不能为负数")
        if isinstance(start_at, bool) or not isinstance(start_at, int) or start_at < 0:
            raise ValueError("start_at 必须是非负整数")
        self._lock = threading.RLock()
        self.cooldown_seconds = cooldown_seconds
        # 已知出口（当前 + 退场）按公网 IP 索引；当前出口由 _current_ip 指向。
        self._egresses: dict[str, PublicConfig] = {}
        self._current_ip: str | None = None
        self._now = start_at
        self._seq = 0
        self._connections: dict[int, Connection] = {}
        self._active_index: dict[tuple[str, int, Endpoint], int] = {}
        self._cooldowns: dict[tuple[str, int, Endpoint], Cooldown] = {}

    # ------------------------------------------------------------------ 配置与时钟

    def configure(
        self,
        public_ip: Any,
        port_min: Any,
        port_max: Any,
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        """设置公网出口。

        只改公网 IP、端口范围不变时执行“换址”：旧出口退场，已建连接和冷却
        原样保留在旧出口；新连接只从新出口分配。退场期间可切回原 IP，原有
        映射和冷却继续有效。退场未结束时拒绝切到第三个 IP。

        端口范围变化或 replace=True 时沿用旧规则：有演练数据默认拒绝，除非
        调用方显式要求清空。
        """

        with self._lock:
            config = PublicConfig(
                _parse_ip(public_ip, "公网 IP"),
                _parse_port(port_min, "起始端口"),
                _parse_port(port_max, "结束端口"),
            )
            if config.port_min > config.port_max:
                raise NatError(
                    "INVALID_RANGE",
                    f"起始端口 {config.port_min} 不能大于结束端口 {config.port_max}",
                    details={"field": "port_min"},
                )

            self._prune_expired()
            self._prune_empty_egresses()
            current = self._current_config()

            if current is None:
                self._clear_state()
                self._egresses = {config.public_ip: config}
                self._current_ip = config.public_ip
                return self._config_result(True, "configured")

            changed = config != current
            if not changed:
                if replace:
                    self._clear_state()
                    return self._config_result(False, "replaced")
                return self._config_result(False, "unchanged")

            if replace:
                self._clear_state()
                self._egresses = {config.public_ip: config}
                self._current_ip = config.public_ip
                return self._config_result(True, "replaced")

            if (config.port_min, config.port_max) != (current.port_min, current.port_max):
                if self._has_state():
                    raise NatError(
                        "CONFIG_LOCKED",
                        "当前存在演练数据，修改公网端口范围前请先清空演练数据",
                        status=409,
                        details={
                            "active_connections": len(self._connections),
                            "cooldowns": len(self._cooldowns),
                        },
                    )
                # 没有存活映射时允许连同端口范围一起改；旧出口无需退场。
                self._clear_state()
                self._egresses = {config.public_ip: config}
                self._current_ip = config.public_ip
                return self._config_result(True, "reconfigured")

            # 端口范围不变、只改公网 IP —— 换址演练。
            target = self._egresses.get(config.public_ip)
            if (
                target is not None
                and (target.port_min, target.port_max)
                == (current.port_min, current.port_max)
            ):
                # 切回一个已知出口（通常是原出口）。它的映射与冷却原样保留。
                previous_ip = current.public_ip
                del self._egresses[previous_ip]
                self._egresses[target.public_ip] = target
                self._current_ip = target.public_ip
                if self._egress_has_state(previous_ip):
                    self._egresses[previous_ip] = current
                self._prune_empty_egresses()
                return self._config_result(True, "switched_back", previous_ip)

            retiring = self._retiring_configs()
            if retiring:
                raise NatError(
                    "EGRESS_RETIRING",
                    (
                        f"出口 {current.public_ip} 的退场尚未结束，不能切换到第三个"
                        f"公网 IP {config.public_ip}；请等待退场出口的连接全部关闭、"
                        "冷却全部到期"
                    ),
                    status=409,
                    details={
                        "current": current.to_dict(),
                        "retiring": [egress.to_dict() for egress in retiring],
                        "requested": config.public_ip,
                    },
                )

            previous_ip = current.public_ip
            if self._egress_has_state(previous_ip):
                # 旧出口仍有连接或冷却，进入退场；否则直接替换。
                self._egresses[previous_ip] = current
            else:
                self._egresses.pop(previous_ip, None)
            self._egresses[config.public_ip] = config
            self._current_ip = config.public_ip
            return self._config_result(True, "switched", previous_ip)

    def reset(self) -> dict[str, Any]:
        """清空连接与冷却，移除退场出口，保留当前出口配置和演练时钟。"""

        with self._lock:
            removed = len(self._connections) + len(self._cooldowns)
            self._clear_state()
            return {"removed": removed}

    def advance(self, seconds: Any) -> dict[str, Any]:
        """推进演练时钟，清理已到期冷却，并自动移除已排空的退场出口。"""

        amount = _parse_seconds(seconds)
        with self._lock:
            self._now += amount
            before = len(self._cooldowns)
            egresses_before = len(self._egresses)
            self._prune_expired()
            self._prune_empty_egresses()
            return {
                "advanced_seconds": amount,
                "now": self._now,
                "now_label": format_elapsed(self._now),
                "expired_cooldowns": before - len(self._cooldowns),
                "removed_egresses": egresses_before - len(self._egresses),
            }

    # ------------------------------------------------------------------ 建连与关闭

    def open_connection(
        self,
        internal_ip: Any,
        internal_port: Any,
        upstream_ip: Any,
        upstream_port: Any,
    ) -> dict[str, Any]:
        """模拟内网端点向指定上游发起连接，返回分到的公网出口与端口。

        整个分配过程在锁内完成：并发的换址要么发生在这次分配之前，要么发生
        在之后，不会出现一条连接同时属于两个出口的中间态。
        """

        with self._lock:
            self._prune_expired()
            self._prune_empty_egresses()
            config = self._current_config()
            if config is None:
                raise NatError(
                    "CONFIG_MISSING",
                    "请先设置公网 IP 与端口范围",
                    status=409,
                )
            internal = Endpoint(
                _parse_ip(internal_ip, "内网 IP"),
                _parse_port(internal_port, "内网端口"),
            )
            upstream = Endpoint(
                _parse_ip(upstream_ip, "上游 IP"),
                _parse_port(upstream_port, "上游端口"),
            )

            for port in range(config.port_min, config.port_max + 1):
                key = (config.public_ip, port, upstream)
                if key not in self._active_index and key not in self._cooldowns:
                    self._seq += 1
                    connection = Connection(
                        id=self._seq,
                        internal=internal,
                        upstream=upstream,
                        egress_ip=config.public_ip,
                        public_port=port,
                        created_at=self._now,
                    )
                    self._connections[connection.id] = connection
                    self._active_index[key] = connection.id
                    return self._connection_dict(connection)

            raise self._exhausted_error(upstream)

    def close_connection(self, connection_id: Any) -> dict[str, Any]:
        """关闭连接，并让 (公网 IP, 公网端口, 原上游) 进入冷却。"""

        with self._lock:
            if isinstance(connection_id, bool):
                raise NatError("INVALID_INPUT", "连接编号必须是整数")
            if isinstance(connection_id, int):
                conn_id = connection_id
            elif isinstance(connection_id, str) and _is_ascii_digits(connection_id.strip()):
                conn_id = int(connection_id.strip())
            elif isinstance(connection_id, float) and connection_id.is_integer():
                conn_id = int(connection_id)
            else:
                raise NatError("INVALID_INPUT", "连接编号必须是整数")

            self._prune_expired()
            connection = self._connections.get(conn_id)
            if connection is None:
                raise NatError("CONNECTION_NOT_FOUND", "连接不存在或已经关闭", status=404)

            closed = self._connection_dict(connection)
            del self._connections[conn_id]
            self._active_index.pop(
                (connection.egress_ip, connection.public_port, connection.upstream), None
            )
            cooldown = Cooldown(
                egress_ip=connection.egress_ip,
                public_port=connection.public_port,
                upstream=connection.upstream,
                internal=connection.internal,
                connection_id=connection.id,
                closed_at=self._now,
                until=self._now + self.cooldown_seconds,
            )
            self._cooldowns[(cooldown.egress_ip, cooldown.public_port, cooldown.upstream)] = cooldown
            return {
                "closed": closed,
                "cooldown": self._cooldown_dict(cooldown),
            }

    # ------------------------------------------------------------------ 回包模拟

    def simulate_reply(
        self,
        upstream_ip: Any,
        upstream_port: Any,
        public_port: Any,
        public_ip: Any = None,
    ) -> dict[str, Any]:
        """模拟上游向公网 IP + 端口回包，返回送达的内网端点或丢弃原因。

        public_ip 省略（None/空串）时按当前出口查询；显式指定时按“IP + 端口 +
        上游端点”在对应出口查询。退场出口上的映射和冷却与当前出口同等生效。
        """

        with self._lock:
            self._prune_expired()
            self._prune_empty_egresses()
            current = self._current_config()
            if current is None:
                raise NatError(
                    "CONFIG_MISSING",
                    "请先设置公网 IP 与端口范围",
                    status=409,
                )
            upstream = Endpoint(
                _parse_ip(upstream_ip, "上游 IP"),
                _parse_port(upstream_port, "上游端口"),
            )
            port = _parse_port(public_port, "公网端口")
            target_ip = _parse_optional_ip(public_ip, "目标公网 IP") or current.public_ip
            target = self._egresses.get(target_ip)

            base: dict[str, Any] = {
                "public": {
                    "ip": target_ip,
                    "port": port,
                    "label": format_host_port(target_ip, port),
                },
                "upstream": upstream.to_dict(),
                "at": self._now,
                "at_label": format_elapsed(self._now),
            }

            if target is None:
                return {
                    "outcome": "dropped",
                    "reason_code": "NO_MAPPING",
                    "reason": (
                        f"公网 IP {target_ip} 不是当前或退场出口，"
                        f"端口 {port} 上不存在与上游 {upstream.label} 的映射"
                    ),
                    "egress_known": False,
                    **base,
                }

            key = (target_ip, port, upstream)

            conn_id = self._active_index.get(key)
            if conn_id is not None:
                connection = self._connections[conn_id]
                return {
                    "outcome": "delivered",
                    "connection_id": connection.id,
                    "internal": connection.internal.to_dict(),
                    "egress_retiring": target_ip != self._current_ip,
                    **base,
                }

            cooldown = self._cooldowns.get(key)
            if cooldown is not None:
                remaining = max(0, cooldown.until - self._now)
                return {
                    "outcome": "dropped",
                    "reason_code": "COOLDOWN_LATE",
                    "reason": (
                        f"公网 {format_host_port(target_ip, port)} 与原上游 "
                        f"{upstream.label} 的映射仍在冷却（剩余 {remaining} 秒），迟包已丢弃"
                    ),
                    "original_internal": cooldown.internal.to_dict(),
                    "connection_id": cooldown.connection_id,
                    "closed_at": cooldown.closed_at,
                    "closed_at_label": format_elapsed(cooldown.closed_at),
                    "cooldown_until": cooldown.until,
                    "cooldown_until_label": format_elapsed(cooldown.until),
                    "remaining_seconds": remaining,
                    "egress_retiring": target_ip != self._current_ip,
                    **base,
                }

            if not target.port_min <= port <= target.port_max:
                return {
                    "outcome": "dropped",
                    "reason_code": "PORT_OUT_OF_RANGE",
                    "reason": (
                        f"公网端口 {port} 不在出口 {target_ip} 的配置范围 "
                        f"{target.port_min}-{target.port_max} 内，回包已丢弃"
                    ),
                    "port_min": target.port_min,
                    "port_max": target.port_max,
                    **base,
                }

            other_upstreams = sorted(
                {
                    connection.upstream
                    for connection in self._connections.values()
                    if connection.egress_ip == target_ip and connection.public_port == port
                },
                key=lambda endpoint: (endpoint.ip, endpoint.port),
            )
            cooling_upstreams = sorted(
                {
                    cooldown.upstream
                    for cooldown in self._cooldowns.values()
                    if cooldown.egress_ip == target_ip and cooldown.public_port == port
                },
                key=lambda endpoint: (endpoint.ip, endpoint.port),
            )
            if other_upstreams:
                labels = "、".join(endpoint.label for endpoint in other_upstreams)
                return {
                    "outcome": "dropped",
                    "reason_code": "UPSTREAM_MISMATCH",
                    "reason": (
                        f"公网 {format_host_port(target_ip, port)} 当前映射到上游 {labels}，"
                        f"与来源 {upstream.label} 不匹配，回包已丢弃"
                    ),
                    "current_upstreams": [endpoint.to_dict() for endpoint in other_upstreams],
                    "cooling_upstreams": [endpoint.to_dict() for endpoint in cooling_upstreams],
                    **base,
                }

            if cooling_upstreams:
                labels = "、".join(endpoint.label for endpoint in cooling_upstreams)
                return {
                    "outcome": "dropped",
                    "reason_code": "NO_MAPPING",
                    "reason": (
                        f"公网 {format_host_port(target_ip, port)} 没有与上游 "
                        f"{upstream.label} 的映射；该端口正为上游 {labels} 冷却，"
                        "与本回包来源无关"
                    ),
                    "cooling_upstreams": [endpoint.to_dict() for endpoint in cooling_upstreams],
                    **base,
                }

            return {
                "outcome": "dropped",
                "reason_code": "NO_MAPPING",
                "reason": (
                    f"公网 {format_host_port(target_ip, port)} 上不存在与上游 "
                    f"{upstream.label} 的映射（从未分配，或冷却已经结束）"
                ),
                **base,
            }

    # ------------------------------------------------------------------ 快照

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._prune_expired()
            self._prune_empty_egresses()
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        config = self._current_config()
        connections = sorted(
            self._connections.values(),
            key=lambda item: (
                item.egress_ip,
                item.public_port,
                item.upstream.ip,
                item.upstream.port,
                item.id,
            ),
        )
        cooldowns = sorted(
            self._cooldowns.values(),
            key=lambda item: (
                item.until,
                item.egress_ip,
                item.public_port,
                item.upstream.ip,
                item.upstream.port,
            ),
        )

        # 上游占用统计只针对当前出口：新连接和端口耗尽判断都只看当前出口。
        current_ip = self._current_ip
        current_connections = [
            connection for connection in connections if connection.egress_ip == current_ip
        ]
        current_cooldowns = [
            cooldown for cooldown in cooldowns if cooldown.egress_ip == current_ip
        ]
        upstreams = sorted(
            {connection.upstream for connection in current_connections}
            | {cooldown.upstream for cooldown in current_cooldowns},
            key=lambda endpoint: (endpoint.ip, endpoint.port),
        )
        upstream_summaries = []
        for upstream in upstreams:
            active_ports = sorted(
                connection.public_port
                for connection in current_connections
                if connection.upstream == upstream
            )
            cooling_ports = sorted(
                cooldown.public_port
                for cooldown in current_cooldowns
                if cooldown.upstream == upstream
            )
            blocked = len(active_ports) + len(cooling_ports)
            capacity = config.size if config is not None else 0
            upstream_summaries.append(
                {
                    "public_ip": current_ip,
                    "upstream": upstream.to_dict(),
                    "active_count": len(active_ports),
                    "cooling_count": len(cooling_ports),
                    "blocked_count": blocked,
                    "available_count": max(0, capacity - blocked),
                    "capacity": capacity,
                    "active_ports": active_ports,
                    "cooling_ports": cooling_ports,
                    "exhausted": config is not None and blocked >= capacity,
                }
            )

        egresses = [self._egress_dict(egress) for egress in self._ordered_egresses()]
        current_egress = next((item for item in egresses if item["current"]), None)
        retiring_egresses = [item for item in egresses if item["retiring"]]

        return {
            "config": config.to_dict() if config is not None else None,
            "current_egress": current_egress,
            "retiring_egresses": retiring_egresses,
            "egresses": egresses,
            "clock": {
                "now": self._now,
                "label": format_elapsed(self._now),
                "cooldown_seconds": self.cooldown_seconds,
            },
            "connections": [self._connection_dict(connection) for connection in connections],
            "cooldowns": [self._cooldown_dict(cooldown) for cooldown in cooldowns],
            "upstreams": upstream_summaries,
            "summary": {
                "active_connections": len(connections),
                "cooldowns": len(cooldowns),
                "upstreams": len(upstream_summaries),
                "egresses": len(egresses),
                "retiring_egresses": len(retiring_egresses),
            },
        }

    # ------------------------------------------------------------------ 内部工具

    def _current_config(self) -> PublicConfig | None:
        if self._current_ip is None:
            return None
        return self._egresses.get(self._current_ip)

    def _ordered_egresses(self) -> list[PublicConfig]:
        return sorted(
            self._egresses.values(),
            key=lambda egress: (egress.public_ip != self._current_ip, egress.public_ip),
        )

    def _retiring_configs(self) -> list[PublicConfig]:
        return [
            egress
            for egress in self._ordered_egresses()
            if egress.public_ip != self._current_ip
        ]

    def _egress_has_state(self, public_ip: str) -> bool:
        return any(
            connection.egress_ip == public_ip for connection in self._connections.values()
        ) or any(cooldown.egress_ip == public_ip for cooldown in self._cooldowns.values())

    def _egress_dict(self, egress: PublicConfig) -> dict[str, Any]:
        active_ports = sorted(
            connection.public_port
            for connection in self._connections.values()
            if connection.egress_ip == egress.public_ip
        )
        cooling_ports = sorted(
            cooldown.public_port
            for cooldown in self._cooldowns.values()
            if cooldown.egress_ip == egress.public_ip
        )
        return {
            **egress.to_dict(),
            "current": egress.public_ip == self._current_ip,
            "retiring": egress.public_ip != self._current_ip,
            "active_connections": len(active_ports),
            "cooldowns": len(cooling_ports),
            "active_ports": active_ports,
            "cooling_ports": cooling_ports,
        }

    def _config_result(
        self,
        changed: bool,
        action: str,
        previous_public_ip: str | None = None,
    ) -> dict[str, Any]:
        config = self._current_config()
        return {
            "config": config.to_dict() if config is not None else None,
            "changed": changed,
            "action": action,
            "previous_public_ip": previous_public_ip,
            "current_egress": (
                self._egress_dict(config) if config is not None else None
            ),
            "retiring_egresses": [
                self._egress_dict(egress) for egress in self._retiring_configs()
            ],
        }

    def _connection_dict(self, connection: Connection) -> dict[str, Any]:
        public = {
            "ip": connection.egress_ip,
            "port": connection.public_port,
            "label": format_host_port(connection.egress_ip, connection.public_port),
            "egress_retiring": connection.egress_ip != self._current_ip,
        }
        return {
            "id": connection.id,
            "internal": connection.internal.to_dict(),
            "upstream": connection.upstream.to_dict(),
            "egress_ip": connection.egress_ip,
            "egress_retiring": connection.egress_ip != self._current_ip,
            "public": public,
            "created_at": connection.created_at,
            "created_label": format_elapsed(connection.created_at),
            "age_seconds": self._now - connection.created_at,
        }

    def _cooldown_dict(self, cooldown: Cooldown) -> dict[str, Any]:
        public = {
            "ip": cooldown.egress_ip,
            "port": cooldown.public_port,
            "label": format_host_port(cooldown.egress_ip, cooldown.public_port),
        }
        return {
            "public": public,
            "public_port": cooldown.public_port,
            "egress_ip": cooldown.egress_ip,
            "egress_retiring": cooldown.egress_ip != self._current_ip,
            "upstream": cooldown.upstream.to_dict(),
            "internal": cooldown.internal.to_dict(),
            "connection_id": cooldown.connection_id,
            "closed_at": cooldown.closed_at,
            "closed_at_label": format_elapsed(cooldown.closed_at),
            "cooldown_until": cooldown.until,
            "cooldown_until_label": format_elapsed(cooldown.until),
            "remaining_seconds": max(0, cooldown.until - self._now),
        }

    def _exhausted_error(self, upstream: Endpoint) -> NatError:
        config = self._current_config()
        assert config is not None
        active_ports = sorted(
            connection.public_port
            for connection in self._connections.values()
            if connection.upstream == upstream and connection.egress_ip == config.public_ip
        )
        cooling_ports = sorted(
            cooldown.public_port
            for cooldown in self._cooldowns.values()
            if cooldown.upstream == upstream and cooldown.egress_ip == config.public_ip
        )
        blocked = len(active_ports) + len(cooling_ports)
        return NatError(
            "NO_PORT_AVAILABLE",
            (
                f"上游 {upstream.label} 在当前出口 {config.public_ip} 的公网端口已耗尽："
                f"在用 {len(active_ports)} 个，冷却 {len(cooling_ports)} 个"
                f"（范围 {config.port_min}-{config.port_max}，共 {config.size} 个）"
            ),
            status=409,
            details={
                "public_ip": config.public_ip,
                "upstream": upstream.to_dict(),
                "port_min": config.port_min,
                "port_max": config.port_max,
                "capacity": config.size,
                "active_count": len(active_ports),
                "cooling_count": len(cooling_ports),
                "blocked_count": blocked,
                "active_ports": active_ports,
                "cooling_ports": cooling_ports,
                "hint": "推进演练时钟待冷却结束后重试，或扩大公网端口范围",
            },
        )

    def _prune_expired(self) -> None:
        expired = [
            key for key, cooldown in self._cooldowns.items() if cooldown.until <= self._now
        ]
        for key in expired:
            del self._cooldowns[key]

    def _prune_empty_egresses(self) -> None:
        """自动移除已排空的退场出口；当前出口永远保留。"""

        for public_ip in list(self._egresses):
            if public_ip == self._current_ip:
                continue
            if not self._egress_has_state(public_ip):
                del self._egresses[public_ip]

    def _has_state(self) -> bool:
        return bool(self._connections or self._cooldowns)

    def _clear_state(self) -> None:
        self._connections.clear()
        self._active_index.clear()
        self._cooldowns.clear()
        # 退场出口随演练数据一起清掉；当前出口保留。
        if self._current_ip is not None and self._current_ip in self._egresses:
            self._egresses = {self._current_ip: self._egresses[self._current_ip]}
        else:
            self._egresses = {}
        # 故意不重置 _seq：连接编号保持单调递增，避免清空后用旧编号误关新连接。
