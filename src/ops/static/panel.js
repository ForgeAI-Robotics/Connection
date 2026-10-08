const STATE_LABEL = {
  checking: "检查中",
  unknown: "状态未知",
  tmux: "运行中",
  unmanaged: "未托管",
  stopped: "已停止",
  up: "通",
  down: "不通",
};

const REMOTE_ACTION_LABEL = {restart: "重启",
  start: "启动",
  stop: "停止",
  "stand-enter": "站立 Enter",
};

let selectedLayer = "brain";
let selected = "master";
let busy = false;
let snapshot = JSON.parse(document.getElementById("initial-services").textContent);
let logToken = 0;
let logAbort = null;
const statusInFlight = new Set();
let overviewInFlight = false;
let activeLogRequest = "";
let logPath = "";
let reviewWasReady = null;
let reviewOpened = false;
let brainOverview = {};
let renderedLog = "";

const TASK_STATE = {running: "执行中", verifying: "核验中", succeeded: "已完成", failed: "已失败",
  cancelled: "已取消", cancelling: "取消确认中", paused: "已暂停", waiting_human: "等待人工处理",
  recovery_required: "待恢复"};

function $(id) {
  return document.getElementById(id);
}

function layerItems() {
  return snapshot[selectedLayer] || [];
}

function logItems() {
  if (selectedLayer === "environment" && $("execution-support").open) {
    return [...layerItems(), ...(snapshot.support || [])];
  }
  return layerItems();
}

function ensureSelection() {
  const items = logItems();
  if (!items.some((item) => item.id === selected)) {
    selected = items[0]?.id || "";
    logPath = "";
  }
}

function currentItem() {
  return logItems().find((item) => item.id === selected);
}

function selectLayer(layer) {
  selectedLayer = layer;
  if (layer === "brain") {
    selected = "master";
  }
  ensureSelection();
  logPath = "";
  document.querySelectorAll(".layer").forEach((button) => {
    button.classList.toggle("active", button.dataset.layer === layer);
  });
  render(true);
  showLogPlaceholder();
  loadLogs(true);
  refresh();
}

function selectService(id) {
  if (!logItems().some((item) => item.id === id)) return;
  if (selected === id) {
    loadLogs(true);
    return;
  }
  selected = id;
  logPath = "";
  markActive();
  showLogPlaceholder();
  loadLogs(true);
}

function dots(items, targetId) {
  const host = $(targetId);
  host.innerHTML = "";
  items.forEach((item) => {
    const dot = document.createElement("span");
    dot.className = `dot ${item.state}`;
    dot.title = `${item.name} ${STATE_LABEL[item.state] || item.state}`;
    host.appendChild(dot);
  });
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function healthLine(item) {
  if (item.id === "master") {
    if (brainOverview.errors?.health) return "大脑健康接口不可达";
    const health = brainOverview.health;
    if (health) return health.ready && health.state === "healthy" ? "大脑已就绪" : "大脑尚未就绪";
  }
  const health = item.health || {};
  const latency = health.latency_ms != null ? `${health.latency_ms}ms` : "";
  return [health.detail, latency].filter(Boolean).join(" · ");
}

// 9882 从不通变通时自动弹一次；浏览器拦弹窗就用卡片上的按钮。
function popReviewPage(robot) {
  const dream = robot.find((item) => item.id === "dream");
  if (!dream || !dream.review_url) return;
  const ready = Boolean(dream.review_ok);
  const known = reviewWasReady;
  reviewWasReady = ready;
  if (!ready || known === null || known === true || reviewOpened) return;
  reviewOpened = true;
  const opened = window.open(dream.review_url, "dream-review");
  if (!opened) {
    const status = $("action-status");
    status.hidden = false;
    status.textContent =
      `9882 审核页已就绪：${dream.review_url}\n` +
      "浏览器拦住了自动弹窗，点卡片上的「打开 9882」。";
  }
}

function card(item) {
  const el = document.createElement("article");
  el.className = "card" + (item.id === selected ? " active" : "");
  el.dataset.id = item.id;
  el.dataset.state = item.state;
  el.dataset.review = String(item.review_ok);
  const entry = item.id === "deploy" || item.id === "feishu" || item.id === "voice";
  el.classList.toggle("entry-card", entry);
  el.onclick = () => selectService(item.id);
  const port = item.port ? `端口 ${item.port}` : "无本地端口";
  const line = healthLine(item);
  el.innerHTML = `
    <div class="card-top">
      <span class="name">${escapeHtml(item.name)}</span>
      <span class="badge ${escapeHtml(item.state)}">${escapeHtml(STATE_LABEL[item.state] || item.state)}</span>
    </div>
    <div class="meta">
      <div>${entry ? "任务入口 · 共用大脑" : escapeHtml(port)}</div>
      ${item.health && item.health.url ? `<div class="detail">${escapeHtml(item.health.url)}</div>` : ""}
      ${item.note ? `<div class="detail">${escapeHtml(item.note)}</div>` : ""}
      <div class="health">${escapeHtml(line)}</div>
    </div>
  `;
  const actions = document.createElement("div");
  actions.className = "actions";
  let hasActions = false;
  if (item.controllable) {
    const running = item.state !== "stopped";
    if (entry) {
      actions.append(actionButton(running ? "关闭入口" : "开启入口", () => act(item, running ? "stop" : "start"), running));
      if (item.id === "deploy") {
        const link = document.createElement("a");
        const url = new URL(window.location.href);
        url.port = String(item.port);
        url.pathname = "/";
        url.search = "";
        url.hash = "";
        link.onclick = (event) => event.stopPropagation();
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener";
        link.className = "service-link";
        link.textContent = "打开网页 ↗";
        actions.append(link);
      }
    } else {
      actions.append(actionButton(running ? "重启" : "启动", () => act(item, running ? "restart" : "start"), running && item.confirm_restart));
      const stop = actionButton("停止", () => act(item, "stop"), true);
      stop.disabled = !running;
      actions.append(stop);
    }
    hasActions = true;
  }
  (item.remote_actions || []).forEach((action) => {
    const label = REMOTE_ACTION_LABEL[action] || action;
    actions.append(actionButton(label, () => act(item, action), action !== "start"));
    hasActions = true;
  });
  if (item.review_url) {
    const button = actionButton("打开 9882", () => {
      window.open(item.review_url, "dream-review");
    }, false);
    button.disabled = !item.review_ok;
    button.title = item.review_ok
      ? item.review_url
      : "9882 还没起来（要等两次 Enter 进 POSE 站立之后）";
    if (!item.review_ok) button.classList.add("disabled");
    actions.append(button);
    hasActions = true;
  }
  if (item.disabled_action) {
    const button = actionButton(item.disabled_action, () => {}, false);
    button.disabled = true;
    button.title = item.disabled_reason || "";
    button.classList.add("disabled");
    actions.append(button);
    hasActions = true;
  }
  if (hasActions) el.appendChild(actions);
  if (["checking", "unknown"].includes(item.state)) {
    actions.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  }
  if (item.id === "master") {
    el.classList.add("brain-primary");
    const summary = document.createElement("div");
    summary.className = "brain-summary";
    el.insertBefore(summary, actions);
    updateBrainSummary(el);
  }
  return el;
}

function updateBrainSummary(el) {
  const host = el.querySelector(".brain-summary");
  if (!host) return;
  const task = brainOverview.task || {};
  const profile = brainOverview.profile || {};
  const mode = profile.config?.mode;
  const route = profile.routes?.reception;
  const modeText = mode === "real" ? "真机" : mode === "simulation" ? "仿真" : "未知";
  const unfinished = task.task_id && !["succeeded", "failed", "cancelled", "completed_hand_state_only"].includes(task.state);
  const taskText = brainOverview.errors?.task ? "无法读取任务状态" : task.active || unfinished
    ? `${TASK_STATE[task.state] || task.state || "处理中"} · ${task.task_id || ""}`
    : task.task_id ? `无活动任务 · 上一任务${TASK_STATE[task.state] || task.state || ""}` : "无活动任务";
  host.textContent = `${modeText}${route?.backend === "reception_protocol" ? " · 协议模拟" : ""} · ${taskText}`;
  if (!brainOverview.health) host.textContent = "正在读取状态…";
  host.title = host.textContent;
  const badge = el.querySelector(".badge");
  if (badge && brainOverview.health) {
    const ready = brainOverview.health.ready && brainOverview.health.state === "healthy";
    badge.textContent = ready ? "已就绪" : "未就绪";
    badge.className = `badge ${ready ? "up" : "down"}`;
  }
}

function tab(item) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "tab" + (item.id === selected ? " active" : "");
  button.dataset.id = item.id;
  button.textContent = item.name;
  button.onclick = () => selectService(item.id);
  return button;
}

function actionButton(label, fn, danger) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  if (danger) button.className = "danger";
  button.onclick = (event) => {
    event.stopPropagation();
    fn();
  };
  return button;
}

function markActive() {
  document.querySelectorAll(".card").forEach((el) => {
    el.classList.toggle("active", el.dataset.id === selected);
  });
  document.querySelectorAll(".tab").forEach((el) => {
    el.classList.toggle("active", el.dataset.id === selected);
  });
  const current = currentItem();
  $("log-title").textContent = current ? `${current.name} 日志` : "运行日志";
  $("log-meta").textContent = logPath;
}

function showLogPlaceholder() {
  const log = $("log");
  log.classList.remove("error");
  log.classList.add("loading");
  log.textContent = "加载中…";
  renderedLog = "";
}

function patchCard(el, item) {
  // State changes also change the entry toggle and remote review button.
  if (el.dataset.state !== item.state || el.dataset.review !== String(item.review_ok)) {
    el.replaceWith(card(item));
    return;
  }
  el.classList.toggle("active", item.id === selected);
  const badge = el.querySelector(".badge");
  if (badge) {
    badge.className = `badge ${item.state}`;
    badge.textContent = STATE_LABEL[item.state] || item.state;
  }
  const health = el.querySelector(".health");
  if (health) health.textContent = healthLine(item);
  if (item.id === "master") updateBrainSummary(el);
}

function render(full) {
  const items = layerItems();
  const environment = selectedLayer === "environment";
  const brain = selectedLayer === "brain";
  $("layer-title").textContent = {brain: "大脑层", environment: "运行环境", robot: "真机层"}[selectedLayer];
  $("layer-count").textContent = brain ? `1 个大脑 · ${items.filter((item) => item.id !== "master").length} 个输入通道` : `${items.length} 个${environment ? "仿真" : "真机"}服务`;
  $("layer-hint").textContent = {
    brain: "点击上方卡片查看对应服务的运行日志。",
    environment: "统一配置仿真与真机，也可按模块单独选择。仿真服务在这里启停。",
    robot: "DREAM 导航与 VLA 操控。启停服务不会发布「开始接待」任务。",
  }[selectedLayer];
  document.querySelector(".workspace").classList.toggle("environment", environment);
  $("execution-settings").hidden = !environment;
  $("execution-support").hidden = !environment;
  $("services-title").hidden = !environment;
  $("tabs").hidden = brain;
  document.querySelector(".workspace").classList.toggle("brain", brain);
  $("layer-hint").hidden = brain;
  renderCards($("cards"), items, full);
  $("log-pin").hidden = true;
  $("log-pin").textContent = "";
  if (environment) renderCards($("support-cards"), snapshot.support || [], full);
  const tabs = $("tabs");
  const logs = logItems();
  const tabIds = logs.map((item) => item.id).join(",");
  if (full || tabs.dataset.ids !== tabIds) {
    tabs.replaceChildren(...logs.map(tab));
    tabs.dataset.ids = tabIds;
  }
  markActive();
  dots(snapshot.brain || [], "brain-dots");
  dots(snapshot.environment || [], "environment-dots");
  dots(snapshot.support || [], "support-dots");
  dots(snapshot.robot || [], "robot-dots");
}

function renderCards(cards, items, full) {
  const ids = items.map((item) => item.id).join(",");
  const needFull =
    full ||
    cards.dataset.layer !== selectedLayer ||
    cards.dataset.ids !== ids ||
    cards.childElementCount !== items.length;
  if (needFull) {
    cards.replaceChildren(...items.map(card));
    cards.dataset.layer = selectedLayer;
    cards.dataset.ids = ids;
  } else {
    items.forEach((item) => {
      const el = cards.querySelector(`[data-id="${item.id}"]`);
      if (el) patchCard(el, item);
    });
  }
}

function stickToBottom(node) {
  return node.scrollHeight - node.scrollTop - node.clientHeight < 48;
}

async function act(item, action) {
  const status = $("action-status");
  if (busy) {
    status.hidden = false;
    status.textContent = "上一次服务操作还在进行，请稍候。";
    return;
  }
  if (action === "restart" && item.confirm_restart) {
    const ok = window.confirm(
      "重启大脑会中断当前进程和正在处理的任务。确定继续？"
    );
    if (!ok) return;
  }
  if (action === "start" && item.confirm_start) {
    const ok = window.confirm(
      item.confirm_start_message || "确定启动该真机服务？"
    );
    if (!ok) return;
  }
  if (action === "stop" && item.confirm_stop) {
    const ok = window.confirm(
      item.confirm_stop_message || "确定停止该真机服务？"
    );
    if (!ok) return;
  }
  const extraConfirm = (item.action_confirms || {})[action];
  if (extraConfirm) {
    if (!window.confirm(extraConfirm)) return;
  }
  busy = true;
  if (selectedLayer !== "brain") selectService(item.id);
  status.hidden = false;
  status.classList.remove("error");
  status.textContent =
    item.id === "dream" && action === "start"
      ? "已收到启动。脚本会跑在导航机 tmux g1_panel_oneclick 里：preflight → READY → SONIC / DREAM / VLA HTTP。出现两次 Enter 提示后必须 10 分钟内按完，否则脚本会锁定导航退出。"
      : item.id === "dream" && action === "stand-enter"
        ? "已向导航机 adapter 窗口发 1 个 Enter，等它回显 …"
      : item.id === "dream" && action === "stop"
        ? "已收到停止。正在关闭 DREAM 与 VLA HTTP/relay，保留 NX SONIC …"
        : item.id === "vla" && action === "start"
          ? "已收到启动。正在按文档连接 4090：check → 依赖检查 → HTTP 8091 + 导航 relay …"
          : item.id === "vla" && action === "stop"
            ? "已收到停止。正在 SSH 4090 关闭 HTTP 与导航 relay …"
            : `正在${{start: "启动", stop: "停止", restart: "重启"}[action] || action} ${item.name} …`;
  try {
    const response = await fetch(`/api/services/${item.id}/${action}`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      status.textContent = payload.error || "操作失败";
      status.classList.add("error");
    } else {
      status.textContent = `${item.name}：${{start: "启动", stop: "停止", restart: "重启"}[action] || REMOTE_ACTION_LABEL[action] || action}操作已完成。`;
    }
  } catch (error) {
    status.textContent = String(error);
    status.classList.add("error");
  } finally {
    busy = false;
    refresh();
    loadLogs(true);
  }
}

async function loadLogs(reset) {
  if (busy) return;
  const current = currentItem();
  if (!current) return;
  if (!reset && activeLogRequest === current.id) return;
  activeLogRequest = current.id;
  const token = ++logToken;
  if (logAbort) logAbort.abort();
  logAbort = new AbortController();
  const log = $("log");
  const stick = !reset && stickToBottom(log);
  try {
    const params = new URLSearchParams({lines: "8000"});
    const logs = await fetch(
      `/api/services/${encodeURIComponent(current.id)}/logs?${params}`,
      { signal: logAbort.signal }
    );
    const body = await logs.json();
    if (!logs.ok) throw new Error(body.error || "日志读取失败");
    if (busy || token !== logToken || body.id !== selected) return;
    log.classList.remove("error", "loading");
    const signature = JSON.stringify([current.id, body.text]);
    if (signature !== renderedLog) {
      const scroll = log.scrollTop;
      log.textContent = (body.text || "暂无日志").replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "");
      renderedLog = signature;
      if (!reset && !stick) log.scrollTop = scroll;
    }
    logPath = body.path || "";
    $("log-meta").textContent = logPath;
    if (reset || stick) log.scrollTop = log.scrollHeight;
  } catch (error) {
    if (error.name === "AbortError" || token !== logToken || busy) return;
    log.classList.remove("loading");
    renderedLog = "";
    log.textContent = String(error);
    log.classList.add("error");
  } finally {
    if (token === logToken) activeLogRequest = "";
  }
}

async function refreshLayer(layer) {
  if (statusInFlight.has(layer)) return;
  statusInFlight.add(layer);
  try {
    const response = await fetch(`/api/status?layer=${layer}`, {signal: AbortSignal.timeout(15000)});
    if (!response.ok) throw new Error("服务状态读取失败");
    const data = await response.json();
    snapshot[layer] = data[layer];
    if (layer === "robot") popReviewPage(data.robot || []);
  } catch (error) {
    snapshot[layer] = (snapshot[layer] || []).map((item) => ({...item,
      state: "unknown", health: {ok: null, detail: "状态读取失败"}}));
  } finally {
    statusInFlight.delete(layer);
    render(false);
  }
}

async function refreshOverview() {
  if (overviewInFlight) return;
  overviewInFlight = true;
  try {
    const response = await fetch("/api/brain/overview", {signal: AbortSignal.timeout(5000)});
    if (!response.ok) throw new Error("大脑状态读取失败");
    brainOverview = await response.json();
  } catch (error) {
    brainOverview = {errors: {health: true, task: true},
      alert: {level: "ERROR", message: "无法读取大脑当前状态"}};
  } finally {
    overviewInFlight = false;
    render(false);
  }
}

function refresh() {
  // Each layer completes independently. Logs never wait for health checks.
  refreshLayer(selectedLayer);
  for (const layer of ["brain", "environment", "support", "robot"]) {
    if (layer !== selectedLayer) refreshLayer(layer);
  }
  refreshOverview();
}

document.querySelectorAll(".layer").forEach((button) => {
  button.onclick = () => selectLayer(button.dataset.layer);
});

$("execution-support").ontoggle = () => {
  if (selectedLayer !== "environment") return;
  ensureSelection();
  render(false);
  showLogPlaceholder();
  loadLogs(true);
};

$("server-address").textContent = window.location.host;
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { refresh(); loadLogs(false); }
});

render(true);
showLogPlaceholder();
loadLogs(true);
refresh();
setInterval(() => {
  if (!document.hidden) { refresh(); loadLogs(false); }
}, 3000);
