# TCP NAT 演练台

值班员排查“采集终端连不上游”用的 TCP NAT 演练页。在网页上设一个公网出口
（IP + 端口范围），模拟内网端点向不同上游端点建连，观察分到的公网端口；
再推进演练时钟、从指定上游向公网端口打回包，看送达的内网端点或丢弃原因。

支持出口换址演练：只改公网 IP 时旧出口退场，已建连接保留原公网 IP 继续收包，
新连接只从当前出口分配端口；退场出口排空后自动移除。

## 快速启动

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

冷却时长默认 60 秒（演练时间），可用环境变量调整：

```bash
NATLAB_COOLDOWN_SECONDS=10 NATLAB_PORT=8080 docker compose up --build
```

没有 Docker 时本地直跑：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
gunicorn --bind 127.0.0.1:8000 --workers 1 --threads 8 wsgi:app
```

> 必须单 worker。演练状态保存在进程内存中，多 worker 会各有一张表。
> Compose 里已固定 `--workers 1 --threads 8`，并发安全由核心状态机的锁保证。

## 演练规则（页面实现口径）

| 规则 | 实现 |
| --- | --- |
| 不同上游端点可共用公网端口 | 映射键是 `(公网 IP, 公网端口, 上游端点)` |
| 同一上游端点的连接绝不撞号 | 分配时排除同一出口上该上游所有在用端口 |
| 关闭后 `(公网 IP, 公网端口, 原上游)` 冷却 60 秒 | 冷却表按键隔离，到期自动释放 |
| 其他上游不受冷却影响 | 冷却只锁同出口、原上游该端口 |
| 耗尽时列出该上游在用/冷却端口数 | 409 + `active_count` / `cooling_count` / 端口明细，只统计当前出口 |
| 冷却中的迟包丢弃 | 回包先查在用映射，再查冷却表，命中冷却即 `COOLDOWN_LATE` |
| 可推进演练时钟 | 虚拟秒数，不依赖真实时间，便于复现 |
| 换址后已建连接继续收包 | 连接记录自己的出口 IP；旧出口退场，映射与冷却原样保留 |
| 新连接只从当前出口分配 | 端口池、耗尽统计都只看当前出口 |
| 退场出口排空后自动移除 | 推进时钟或任意操作时清理；当前出口永远保留 |
| 退场期间可切回原 IP | 切回后原出口的映射和冷却继续生效 |
| 退场期间切第三个 IP 被拒 | 409 `EGRESS_RETIRING`，不改变任何状态 |
| 并发建连与换址 | 同一把锁内完成，连接完整落在切换前或切换后的出口 |

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 快照（配置、出口列表、时钟、连接、冷却、当前出口按上游汇总） |
| POST | `/api/config` | 设置 `public_ip` / `port_min` / `port_max`；只改 IP 即换址，`replace=true` 清空后应用 |
| POST | `/api/reset` | 清空连接与冷却、移除退场出口，保留当前配置和时钟 |
| POST | `/api/clock/advance` | `{"seconds": N}` 推进演练时钟，并清理已排空的退场出口 |
| POST | `/api/connections` | `internal_ip/internal_port/upstream_ip/upstream_port` 建连 |
| POST | `/api/connections/<id>/close` | 关闭连接并在其原出口进入冷却 |
| POST | `/api/replies` | `upstream_ip/upstream_port/public_port` + 可选 `public_ip` 模拟回包 |

变更类接口返回 `{"snapshot": ..., "result": ...}`；业务错误返回 4xx +
`{"error": {"code", "message", "details"}}`。

`/api/config` 的 `result.action`：`configured`、`unchanged`、`replaced`、
`reconfigured`（无数据时改端口范围）、`switched`（换址，旧出口退场）、
`switched_back`（切回已知出口）。`switched` / `switched_back` 附带
`previous_public_ip`。

快照中的出口信息：

* `config` / `current_egress`：当前出口。
* `egresses`：当前 + 退场出口列表，每项带 `current` / `retiring`、活动连接数、
  冷却数、在用/冷却端口。
* `retiring_egresses`：还要等排空的出口。
* `connections[].egress_ip` / `egress_retiring`：连接实际使用的出口。
* `upstreams`：只统计当前出口的端口占用与耗尽状态。

回包省略 `public_ip`（或传空串）时查当前出口；显式传入时按
“IP + 端口 + 上游端点”在该出口查映射。命中退场出口的在用映射仍会送达。
丢弃原因码：`COOLDOWN_LATE`、`UPSTREAM_MISMATCH`、`PORT_OUT_OF_RANGE`、
`NO_MAPPING`。切第三个 IP 被拒的错误码为 `EGRESS_RETIRING`。

## 自测

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v   # 后端 44 项
node tests/frontend_smoke.mjs                          # 前端交互 30 项断言
```

`tests/frontend_smoke.mjs` 用最小 DOM 桩加载页面里的真实脚本，按后端返回形状
驱动交互，防止 JS 与 API 字段漂移；没有 Node 时可跳过。

服务启动后可跑换址端到端试跑（同端口回包、冷却中回切、并发换址、第三 IP 拒绝）：

```bash
python3 tests/e2e_switch.py http://127.0.0.1:8000
```

> 该脚本会清空当前演练数据，只在演练台上用。

## 结构

```
natlab/core.py           状态机：分配、复用、冷却、回包、快照
natlab/__init__.py       Flask 应用工厂 + JSON API
natlab/templates/index.html  单页演练台
wsgi.py                  gunicorn 入口
tests/                   后端单测 + 前端桩冒烟 + 换址端到端试跑
Dockerfile / docker-compose.yml
```
