"""对运行中的演练服务做换址端到端试跑（通过 HTTP，不直接调用状态机）。

用法（先按 Docker Compose 等价命令启动服务）：
    python3 tests/e2e_switch.py http://127.0.0.1:8000

覆盖：
1. 换址后同端口回包：旧连接在旧 IP 收包，新连接在新 IP 用同一端口。
2. 冷却中回切：旧出口关闭后仍冷却，切回原 IP 冷却继续生效。
3. 并发换址：建连与换址并发时，每条连接完整落在切换前或切换后。
4. 退场期间切第三个 IP 被拒，状态保持现状。
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, payload: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def post(path: str, payload: dict | None = None):
    return call("POST", path, payload)


def state():
    return call("GET", "/api/state")[1]


def connect(internal: str, upstream: str = "198.51.100.7", port: int = 443):
    return post(
        "/api/connections",
        {
            "internal_ip": internal,
            "internal_port": 51515,
            "upstream_ip": upstream,
            "upstream_port": port,
        },
    )


def reply(upstream: str, port: int, public_port: int, public_ip: str | None = None):
    payload = {"upstream_ip": upstream, "upstream_port": port, "public_port": public_port}
    if public_ip is not None:
        payload["public_ip"] = public_ip
    return post("/api/replies", payload)


def config(ip: str, low: int = 40000, high: int = 40004):
    return post("/api/config", {"public_ip": ip, "port_min": low, "port_max": high})


checks: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    checks.append((name, condition, detail))
    print(("PASS  " if condition else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))


# ---------------------------------------------------------------- 1. 同端口回包
post("/api/reset")
post("/api/clock/advance", {"seconds": 0})
code, body = config("203.0.113.10")
assert code == 200, body
old = connect("10.0.0.21")[1]["result"]
old_other = connect("10.0.0.22", upstream="198.51.100.8")[1]["result"]
check(
    "换址前两个上游共用同一公网端口",
    old["public"]["port"] == old_other["public"]["port"] == 40000,
    f'{old["public"]["label"]} / {old_other["public"]["label"]}',
)

code, body = config("203.0.113.20")
check("换址成功", code == 200 and body["result"]["action"] == "switched", str(body.get("result", {}).get("action")))
check("旧出口进入退场", body["snapshot"]["summary"]["retiring_egresses"] == 1)

new = connect("10.0.0.23")[1]["result"]
check(
    "新连接只从当前出口分配同一端口",
    new["public"]["ip"] == "203.0.113.20" and new["public"]["port"] == 40000,
    new["public"]["label"],
)

_, delivered_old = reply("198.51.100.7", 443, 40000, "203.0.113.10")
_, delivered_new = reply("198.51.100.7", 443, 40000, "203.0.113.20")
check(
    "旧出口同端口回包送达旧连接",
    delivered_old["result"]["outcome"] == "delivered"
    and delivered_old["result"]["internal"]["ip"] == "10.0.0.21",
)
check(
    "新出口同端口回包送达新连接",
    delivered_new["result"]["outcome"] == "delivered"
    and delivered_new["result"]["internal"]["ip"] == "10.0.0.23",
)
_, omitted = reply("198.51.100.7", 443, 40000)
check(
    "省略目标 IP 时查当前出口",
    omitted["result"]["public"]["ip"] == "203.0.113.20"
    and omitted["result"]["internal"]["ip"] == "10.0.0.23",
)
_, routing_old = reply("198.51.100.8", 443, 40000, "203.0.113.10")
check(
    "退场出口按上游端点区分同一端口",
    routing_old["result"]["outcome"] == "delivered"
    and routing_old["result"]["internal"]["ip"] == "10.0.0.22",
)

_, rejected = config("203.0.113.30")
check(
    "退场期间切第三个 IP 被拒且保持现状",
    rejected["error"]["code"] == "EGRESS_RETIRING"
    and state()["config"]["public_ip"] == "203.0.113.20",
    rejected["error"]["code"],
)

# ---------------------------------------------------------------- 2. 冷却中回切
post("/api/reset")
config("203.0.113.10")
first = connect("10.0.0.31")[1]["result"]
post(f'/api/connections/{first["id"]}/close')
config("203.0.113.20")
second = connect("10.0.0.32")[1]["result"]
check(
    "换址后新连接落在新出口",
    second["public"]["ip"] == "203.0.113.20" and second["public"]["port"] == 40000,
    second["public"]["label"],
)

_, late_old = reply("198.51.100.7", 443, 40000, "203.0.113.10")
check(
    "旧出口关闭后仍在旧出口冷却",
    late_old["result"]["reason_code"] == "COOLDOWN_LATE"
    and late_old["result"]["remaining_seconds"] == 60,
    late_old["result"]["reason_code"],
)

code, back = config("203.0.113.10")
check(
    "冷却中可切回原出口",
    code == 200 and back["result"]["action"] == "switched_back",
    back.get("result", {}).get("action"),
)
back_state = back["snapshot"]
check(
    "切回后原出口冷却保留",
    len(back_state["cooldowns"]) == 1
    and back_state["cooldowns"][0]["public"]["ip"] == "203.0.113.10",
    f'cooldowns={len(back_state["cooldowns"])}',
)
check(
    "切回后另一出口有在途连接而退场",
    back_state["summary"]["retiring_egresses"] == 1
    and [item["public_ip"] for item in back_state["retiring_egresses"]] == ["203.0.113.20"],
    f'retiring={[item["public_ip"] for item in back_state["retiring_egresses"]]}',
)
check(
    "切回后原出口已有映射仍保留",
    any(
        conn["public"]["ip"] == "203.0.113.20" and conn["id"] == second["id"]
        for conn in back_state["connections"]
    ),
)

_, still_late = reply("198.51.100.7", 443, 40000)
check(
    "切回后省略目标 IP 命中当前出口的冷却",
    still_late["result"]["public"]["ip"] == "203.0.113.10"
    and still_late["result"]["reason_code"] == "COOLDOWN_LATE",
    still_late["result"]["reason_code"],
)
_, old_delivered = reply("198.51.100.7", 443, 40000, "203.0.113.20")
check(
    "切回后退场出口的在途连接仍可收包",
    old_delivered["result"]["outcome"] == "delivered"
    and old_delivered["result"]["internal"]["ip"] == "10.0.0.32",
)

adv = post("/api/clock/advance", {"seconds": 60})[1]
check("推进时钟释放冷却", adv["result"]["expired_cooldowns"] == 1)
after = adv["snapshot"]
check(
    "推进时钟不误删已切回的当前出口",
    after["config"]["public_ip"] == "203.0.113.10"
    and any(item["current"] for item in after["egresses"]),
    f'egresses={[item["public_ip"] for item in after["egresses"]]}',
)
check(
    "退场出口还有在途连接时不会被移除",
    after["summary"]["retiring_egresses"] == 1,
    f'retiring={after["summary"]["retiring_egresses"]}',
)

post(f'/api/connections/{second["id"]}/close')
removed = post("/api/clock/advance", {"seconds": 60})[1]
check(
    "退场出口连接全关、冷却全到期后自动移除",
    removed["result"]["removed_egresses"] == 1
    and [item["public_ip"] for item in removed["snapshot"]["egresses"]] == ["203.0.113.10"],
    f'egresses={[item["public_ip"] for item in removed["snapshot"]["egresses"]]}',
)

# ---------------------------------------------------------------- 3. 并发换址
post("/api/reset")
config("203.0.113.10", 41000, 41249)
results: list[dict] = []
errors: list[Exception] = []
lock = threading.Lock()
barrier = threading.Barrier(41)


def worker(index: int) -> None:
    barrier.wait()
    try:
        code, body = connect(f"10.3.{index // 250}.{index % 250 + 1}")
        with lock:
            if code == 200:
                results.append(body["result"])
            else:
                errors.append(RuntimeError(str(body)))
    except Exception as exc:  # pragma: no cover
        with lock:
            errors.append(exc)


threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
for thread in threads:
    thread.start()
barrier.wait()
switch_code, switch_body = config("203.0.113.20", 41000, 41249)
for thread in threads:
    thread.join()

ips = {item["public"]["ip"] for item in results}
check("并发换址成功", switch_code == 200 and switch_body["result"]["action"] == "switched")
check(
    "并发建连全部落在切换前或切换后的出口",
    not errors and len(results) == 40 and ips <= {"203.0.113.10", "203.0.113.20"},
    f"n={len(results)}, errors={len(errors)}, ips={sorted(ips)}",
)
keys = [(item["public"]["ip"], item["public"]["port"]) for item in results]
check("并发建连在同一出口内端口不撞号", len(keys) == len(set(keys)), f"unique={len(set(keys))}")
check("每条连接的出口字段与公网字段一致", all(item["public"]["ip"] == item["egress_ip"] for item in results))
concurrent_state = state()
check(
    "快照连接数完整、旧出口退场标记正确",
    concurrent_state["summary"]["active_connections"] == 40
    and concurrent_state["summary"]["retiring_egresses"] == (1 if "203.0.113.10" in ips else 0),
    f'active={concurrent_state["summary"]["active_connections"]}, retiring={concurrent_state["summary"]["retiring_egresses"]}',
)

failed = [name for name, ok, _ in checks if not ok]
print()
print(f"e2e: {len(checks) - len(failed)}/{len(checks)} passed")
if failed:
    print("failed:", failed)
    sys.exit(1)
