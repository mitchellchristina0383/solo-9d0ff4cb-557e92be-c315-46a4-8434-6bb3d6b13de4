// 最小 DOM 桩 + 页面脚本冒烟测试（不需要 jsdom）。
// 用法：node tests/frontend_smoke.mjs [页面 HTML 路径]
// 目的：用真实后端返回形状驱动前端脚本，防止 JS 与 API 字段漂移。

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const htmlPath = process.argv[2] || join(here, "..", "natlab", "templates", "index.html");
const html = readFileSync(htmlPath, "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const ids = [...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);

class El {
  constructor(id = "") {
    this.id = id;
    this.value = "";
    this.checked = false;
    this.innerHTML = "";
    this.textContent = "";
    this.className = "";
    this.dataset = {};
    this.children = [];
    this.handlers = {};
    this.classList = { add: () => {}, remove: () => {} };
  }
  addEventListener(type, fn) { this.handlers[type] = fn; }
  prepend(child) { this.children.unshift(child); }
  removeChild(child) { this.children = this.children.filter((c) => c !== child); }
  get lastChild() { return this.children[this.children.length - 1]; }
  closest() { return this.dataset.close === undefined && this.dataset.switchEgress === undefined ? null : this; }
}

const elements = new Map(ids.map((id) => [id, new El(id)]));
const documentHandlers = {};
globalThis.document = {
  getElementById: (id) => elements.get(id) || new El(id),
  querySelectorAll: () => [],
  addEventListener: (type, fn) => { documentHandlers[type] = fn; },
  createElement: () => new El(),
};
globalThis.confirm = () => true;
globalThis.setTimeout = () => 0;
globalThis.clearTimeout = () => {};

const calls = [];
let responses = [];
globalThis.fetch = async (path, options) => {
  calls.push({ path, body: options && options.body ? JSON.parse(options.body) : null });
  const next = responses.shift();
  if (!next) throw new Error(`no canned response for ${path}`);
  return {
    ok: next.status ? next.status < 400 : true,
    status: next.status || 200,
    json: async () => next.body,
  };
};

const byId = (id) => elements.get(id);
const settle = () => new Promise((resolve) => setImmediate(resolve));

const snapshot = (overrides = {}) => ({
  config: { public_ip: "203.0.113.10", port_min: 40000, port_max: 40001, size: 2, label: "203.0.113.10:40000-40001" },
  current_egress: { public_ip: "203.0.113.10", port_min: 40000, port_max: 40001, size: 2, label: "203.0.113.10:40000-40001", current: true, retiring: false, active_connections: 0, cooldowns: 0, active_ports: [], cooling_ports: [] },
  retiring_egresses: [],
  egresses: [{ public_ip: "203.0.113.10", port_min: 40000, port_max: 40001, size: 2, label: "203.0.113.10:40000-40001", current: true, retiring: false, active_connections: 0, cooldowns: 0, active_ports: [], cooling_ports: [] }],
  clock: { now: 0, label: "T+00:00:00", cooldown_seconds: 60 },
  connections: [],
  cooldowns: [],
  upstreams: [],
  summary: { active_connections: 0, cooldowns: 0, upstreams: 0, egresses: 1, retiring_egresses: 0 },
  ...overrides,
});

const connection = {
  id: 7,
  internal: { ip: "10.0.0.21", port: 51515, label: "10.0.0.21:51515" },
  upstream: { ip: "198.51.100.7", port: 443, label: "198.51.100.7:443" },
  public: { ip: "203.0.113.10", port: 40000, label: "203.0.113.10:40000" },
  created_at: 0,
  created_label: "T+00:00:00",
  age_seconds: 0,
};

function assert(condition, message) {
  if (!condition) {
    console.error(`FAIL: ${message}`);
    process.exitCode = 1;
  } else {
    console.log(`ok - ${message}`);
  }
}

// 1. 首次加载：GET /api/state 并渲染配置。
responses.push({ body: snapshot() });
new Function(script)();
await settle();
assert(calls[0] && calls[0].path === "/api/state", "首屏请求 /api/state");
assert(byId("pool-size").textContent === "2", "端口池大小渲染");
assert(byId("public-ip").value === "203.0.113.10", "公网 IP 回填");

// 2. 建连：POST /api/connections 的 body 取表单，结果回填回包表单。
byId("internal-ip").value = "10.0.0.21";
byId("internal-port").value = "51515";
byId("upstream-ip").value = "198.51.100.7";
byId("upstream-port").value = "443";
responses.push({ body: { snapshot: snapshot({ summary: { active_connections: 1, cooldowns: 0, upstreams: 1 } }), result: connection } });
await byId("connect-form").handlers.submit({ preventDefault() {} });
await settle();
const connectCall = calls.find((c) => c.path === "/api/connections");
assert(connectCall && connectCall.body.internal_ip === "10.0.0.21", "建连请求体正确");
assert(byId("reply-public-port").value === 40000, "建连后回填公网端口");
assert(byId("reply-upstream-ip").value === "198.51.100.7", "建连后回填上游 IP");
assert(byId("log").children.length >= 1, "建连写入日志");

// 3. 回包送达。
responses.push({
  body: {
    snapshot: snapshot(),
    result: {
      outcome: "delivered", connection_id: 7, internal: connection.internal,
      public: { ip: "203.0.113.10", port: 40000, label: "203.0.113.10:40000" },
      upstream: connection.upstream, at: 0, at_label: "T+00:00:00",
    },
  },
});
byId("reply-upstream-ip").value = "198.51.100.7";
byId("reply-upstream-port").value = "443";
byId("reply-public-port").value = "40000";
await byId("reply-form").handlers.submit({ preventDefault() {} });
await settle();
assert(byId("reply-result").innerHTML.includes("送达"), "送达结果渲染");
assert(byId("reply-result").innerHTML.includes("10.0.0.21:51515"), "送达显示内网端点");
assert(byId("reply-result").className.includes("delivered"), "送达样式");

// 4. 冷却中的迟包：丢弃原因渲染。
responses.push({
  body: {
    snapshot: snapshot(),
    result: {
      outcome: "dropped", reason_code: "COOLDOWN_LATE",
      reason: "公网端口 40000 与原上游 198.51.100.7:443 的映射仍在冷却（剩余 30 秒），迟包已丢弃",
      remaining_seconds: 30, original_internal: connection.internal,
      public: { ip: "203.0.113.10", port: 40000, label: "203.0.113.10:40000" },
      upstream: connection.upstream, at: 30, at_label: "T+00:00:30",
    },
  },
});
await byId("reply-form").handlers.submit({ preventDefault() {} });
await settle();
assert(byId("reply-result").innerHTML.includes("COOLDOWN_LATE"), "丢弃原因渲染");
assert(byId("reply-result").innerHTML.includes("冷却"), "丢弃提示包含冷却说明");
assert(byId("reply-result").className.includes("dropped"), "丢弃样式");

// 5. 关闭连接：读取 body.result.cooldown。
responses.push({
  body: {
    snapshot: snapshot({ cooldowns: [{ public: connection.public, public_port: 40000, upstream: connection.upstream, internal: connection.internal, connection_id: 7, closed_at: 0, closed_at_label: "T+00:00:00", cooldown_until: 60, cooldown_until_label: "T+00:01:00", remaining_seconds: 60 }], summary: { active_connections: 0, cooldowns: 1, upstreams: 1 } }),
    result: { closed: connection, cooldown: { public: connection.public, public_port: 40000, upstream: connection.upstream, internal: connection.internal, connection_id: 7, closed_at: 0, closed_at_label: "T+00:00:00", cooldown_until: 60, cooldown_until_label: "T+00:01:00", remaining_seconds: 60 } },
  },
});
const closeBtn = new El();
closeBtn.dataset.close = "7";
await documentHandlers.click({ target: { closest: () => closeBtn } });
await settle();
assert(calls.some((c) => c.path === "/api/connections/7/close"), "关闭请求路径");
assert(byId("cooldowns").innerHTML.includes("203.0.113.10:40000"), "冷却表渲染公网端口");

// 6. 耗尽错误：409 → 日志写明细，不抛未捕获异常。
responses.push({
  status: 409,
  body: { error: { code: "NO_PORT_AVAILABLE", message: "上游 198.51.100.7:443 的公网端口已耗尽：在用 2 个，冷却 0 个", details: { upstream: connection.upstream, active_count: 2, cooling_count: 0, capacity: 2, port_min: 40000, port_max: 40001 } } },
});
await byId("connect-form").handlers.submit({ preventDefault() {} });
await settle();
assert(byId("log").children[0].innerHTML.includes("NO_PORT_AVAILABLE") || byId("log").children[0].innerHTML.includes("耗尽"), "耗尽写入日志");

// 7. 推进时钟：读取 body.result.expired_cooldowns。
responses.push({ body: { snapshot: snapshot({ clock: { now: 60, label: "T+00:01:00", cooldown_seconds: 60 } }), result: { advanced_seconds: 60, now: 60, now_label: "T+00:01:00", expired_cooldowns: 1, removed_egresses: 0 } } });
byId("advance-seconds").value = "60";
await byId("advance-btn").handlers.click();
await settle();
assert(byId("clock").textContent.includes("T+00:01:00"), "时钟渲染");

// 8. 换址：目标 IP 填进回包表单，出口列表展示当前 + 退场。
const retiringEgress = {
  public_ip: "203.0.113.10", port_min: 40000, port_max: 40001, size: 2,
  label: "203.0.113.10:40000-40001", current: false, retiring: true,
  active_connections: 1, cooldowns: 1, active_ports: [40000], cooling_ports: [40001],
};
const currentEgress = {
  public_ip: "203.0.113.20", port_min: 40000, port_max: 40001, size: 2,
  label: "203.0.113.20:40000-40001", current: true, retiring: false,
  active_connections: 0, cooldowns: 0, active_ports: [], cooling_ports: [],
};
responses.push({
  body: {
    snapshot: snapshot({
      config: { public_ip: "203.0.113.20", port_min: 40000, port_max: 40001, size: 2, label: "203.0.113.20:40000-40001" },
      current_egress: currentEgress,
      retiring_egresses: [retiringEgress],
      egresses: [currentEgress, retiringEgress],
      summary: { active_connections: 1, cooldowns: 1, upstreams: 0, egresses: 2, retiring_egresses: 1 },
    }),
    result: { config: { public_ip: "203.0.113.20", port_min: 40000, port_max: 40001, size: 2 }, changed: true, action: "switched", previous_public_ip: "203.0.113.10", current_egress: currentEgress, retiring_egresses: [retiringEgress] },
  },
});
byId("public-ip").value = "203.0.113.20";
byId("port-min").value = "40000";
byId("port-max").value = "40001";
await byId("config-form").handlers.submit({ preventDefault() {} });
await settle();
assert(byId("egress-hint").textContent.includes("203.0.113.20"), "换址后提示当前出口");
assert(byId("egresses").innerHTML.includes("退场中"), "出口列表标记退场");
assert(byId("egresses").innerHTML.includes("切回此出口"), "出口列表提供切回按钮");

// 9. 回包目标 IP：留空不提交字段，填写时按字段提交。
byId("reply-public-ip").value = "";
responses.push({
  body: {
    snapshot: snapshot(),
    result: {
      outcome: "dropped", reason_code: "NO_MAPPING", reason: "当前出口无映射",
      public: { ip: "203.0.113.20", port: 40000, label: "203.0.113.20:40000" },
      upstream: connection.upstream, at: 0, at_label: "T+00:00:00",
    },
  },
});
await byId("reply-form").handlers.submit({ preventDefault() {} });
await settle();
assert(calls.at(-1).body.public_ip === undefined, "留空目标 IP 不提交字段");

byId("reply-public-ip").value = "203.0.113.10";
responses.push({
  body: {
    snapshot: snapshot(),
    result: {
      outcome: "delivered", connection_id: 7, internal: connection.internal, egress_retiring: true,
      public: { ip: "203.0.113.10", port: 40000, label: "203.0.113.10:40000" },
      upstream: connection.upstream, at: 0, at_label: "T+00:00:00",
    },
  },
});
await byId("reply-form").handlers.submit({ preventDefault() {} });
await settle();
assert(calls.at(-1).body.public_ip === "203.0.113.10", "显式目标 IP 提交字段");

// 10. 切回按钮：POST /api/config 使用退场出口 IP 和当前端口范围。
const switchBtn = new El();
switchBtn.dataset.switchEgress = "203.0.113.10";
responses.push({
  body: {
    snapshot: snapshot(),
    result: { config: { public_ip: "203.0.113.10", port_min: 40000, port_max: 40001, size: 2 }, changed: true, action: "switched_back", previous_public_ip: "203.0.113.20" },
  },
});
await documentHandlers.click({ target: { closest: () => switchBtn } });
await settle();
const switchCall = calls.at(-1);
assert(switchCall.path === "/api/config", "切回请求路径");
assert(switchCall.body.public_ip === "203.0.113.10", "切回请求目标 IP");
assert(switchCall.body.port_min === 40000 && switchCall.body.port_max === 40001, "切回沿用当前端口范围");
assert(byId("log").children[0].innerHTML.includes("切回"), "切回写入日志");

// 11. 换址按钮：只取表单 IP，端口范围强制沿用当前快照。
const currentConfig = {
  public_ip: "203.0.113.10", port_min: 40000, port_max: 40001, size: 2,
  label: "203.0.113.10:40000-40001",
};
responses.push({
  body: {
    snapshot: snapshot({ config: { ...currentConfig, public_ip: "203.0.113.30", label: "203.0.113.30:40000-40001" } }),
    result: { config: { ...currentConfig, public_ip: "203.0.113.30" }, changed: true, action: "switched", previous_public_ip: "203.0.113.10" },
  },
});
byId("public-ip").value = "203.0.113.30";
byId("port-min").value = "1";
byId("port-max").value = "2";
await byId("switch-ip-btn").handlers.click();
await settle();
const switchIpCall = calls.at(-1);
assert(switchIpCall.path === "/api/config", "换址按钮请求路径");
assert(switchIpCall.body.public_ip === "203.0.113.30", "换址按钮提交目标 IP");
assert(switchIpCall.body.port_min === 40000 && switchIpCall.body.port_max === 40001, "换址按钮沿用当前端口范围");
assert(byId("port-min").value === 40000, "换址后表单端口范围回填");

console.log(process.exitCode ? "frontend smoke: FAILED" : "frontend smoke: OK");
