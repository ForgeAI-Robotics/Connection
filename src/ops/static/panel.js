const STATE_LABEL = {
  checking: "检查中",
  unknown: "状态未知",
  tmux: "运行中",
  unmanaged: "未托管",
  stopped: "已停止",
  up: "通",
  down: "不通",
  partial: "部分就绪",
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
  const items = snapshot[selectedLayer] || [];
  return selectedLayer === "environment" ? items.filter((item) => item.id === "simple_o7") : items;
}

function logItems() {
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

function vlaModelSummary(runtime) {
  const prep = runtime.preparation;
  if (!prep || prep.enabled !== true) return "开门模型：未启用提前准备";
  const model = prep.operations?.open_fridge || {};
  const labels = {cold: "未准备", checking: "检查中", loading: "加载中", warming: "预热中",
    ready: "已就绪", busy: "使用中", invalidated: "需重新准备", failed: "准备失败", stopping: "停止中"};
  return `开门模型：${model.warm_ready === true ? "已就绪" : labels[model.state] || "未知"}`;
}

function vlaComponentLines(item) {
  const runtime = item.health?.runtime;
  if (!runtime) return ["组件状态尚未核验"];
  const states = {running: "运行中", stopped: "已停止", unknown: "未知", initializing: "初始化中", recovering: "恢复中", blocked: "恢复受阻"};
  const http = runtime.http || {};
  const camera = runtime.camera || {};
  const o6 = runtime.o6 || {};
  const readiness = (part) => part.ready === true ? "检查通过" : part.ready === false ? "未就绪" : "就绪情况未知";
  const frameSummary = camera.frames_verified ? "检查时三路均有新鲜帧"
    : Object.keys(camera.streams || {}).length ? "三路未全部通过，见分路状态" : "尚未核验当前出帧";
  const lines = [
    `HTTP：${states[http.state] || "未知"} · ${readiness(http)}`,
    `实例管理：${http.managed === true ? "已识别为官方启动器实例（手动或面板启动）" : http.managed === false ? "外部实例，未托管" : "归属未核清"}`,
    `相机：${states[camera.state] || "未知"} · ${frameSummary}${camera.reason ? " · " + camera.reason : ""}`,
  ];
  if (typeof camera.managed === "boolean") lines.push(`采集管理：${camera.managed ? "VLA 统一管理" : "已识别外部启动的采集服务"}`);
  for (const [key, label] of [["ego_view", "头部"], ["left_wrist", "左腕"], ["right_wrist", "右腕"]]) {
    const frame = camera.streams?.[key];
    if (!frame || Object.keys(frame).length === 0) {
      lines.push(`${label}：帧状态未知`);
      continue;
    }
    const fps = Number.isFinite(frame.fps) ? ` · ${frame.fps.toFixed(1)} FPS` : "";
    const age = Number.isFinite(frame.age_ms) ? ` · 采集年龄 ${Math.round(frame.age_ms)} ms` : "";
    lines.push(`${label}：${frame.ready === true ? "检查时帧新鲜" : frame.ready === false ? "无新鲜帧" : "未知"}${fps}${age}${frame.reason ? " · " + frame.reason : ""}`);
  }
  lines.push(`O6：${states[o6.state] || "未知"} · ${readiness(o6)}${o6.reason ? " · " + o6.reason : ""}`);
  const recoveryLine = o6RecoverySummary(o6);
  if (recoveryLine) lines.push(recoveryLine);
  lines.push(vlaModelSummary(runtime));
  lines.push(vlaExecutionSummary(runtime));
  lines.push(`停止 HTTP：${http.can_stop === true ? "可请求（执行时再次核验）" : "当前不可停止"}${http.reason ? " · " + http.reason : ""}`);
  if (runtime.observed_at) lines.push(`状态检查时间：${runtime.observed_at}`);
  return lines;
}

function o6FaultSummary(o6 = {}) {
  if (o6.ready === true) return "";
  const fault = o6.feedback_fault || (["stopped", "initializing", "recovering", "blocked"].includes(o6.state) ? o6.last_feedback_fault : null);
  if (!fault || fault.schema !== "fq/o6-feedback-fault/v1") return "";
  const side = {left: "左手", right: "右手"}[fault.side] || "手部";
  const cause = fault.code === "CAN_READ_FAILED"
    ? (fault.errno === 100 ? "CAN 接口断连" : "CAN 读取失败")
    : fault.code === "ENCODER_TIMEOUT" ? "反馈超时" : "反馈故障";
  // A durable fault belongs to its original receiver. It is not proof that
  // the CAN interface is still down, or that a later instance failed likewise.
  return ` · ${o6.state === "running" ? "故障" : "最近故障记录"}：${side}${cause}`;
}

function o6RecoverySummary(o6 = {}) {
  const recovery = o6.recovery;
  if (!recovery || recovery.schema !== "fq/o6-recovery/v1") return "";
  if (recovery.enabled !== true) return "O6 自动恢复：已关闭";
  const labels = {initializing: "初始化中", recovering: "恢复中", blocked: "恢复受阻", stopped: "已停止"};
  let state = recovery.state === "ready" ? (o6.ready === true ? "已就绪" : "待核验") : (labels[recovery.state] || "待核验");
  const reasons = {WAIT_CAN_STABLE: "等待 CAN 稳定", CAN_STABLE_NEW_RECEIVER: "重建接收实例",
    RECOVERY_RATE_LIMIT: "频繁掉线，自动恢复已暂停", RECOVERY_DEADLINE: "等待恢复超时",
    USB_SOURCE_MISMATCH: "USB 设备来源不符", O6_OWNER_UNVERIFIED: "设备归属未核清",
    O6_OTHER_OWNER_PROCESS: "存在其他手部控制进程"};
  const reason = recovery.reason ? (reasons[recovery.reason] || String(recovery.reason).slice(0, 120)) : "";
  const count = Number.isSafeInteger(recovery.restart_count_in_window) && recovery.restart_count_in_window > 0
    ? ` · 窗口内恢复 ${recovery.restart_count_in_window} 次` : "";
  return `O6 自动恢复：${state}${reason ? " · " + reason : ""}${count}`;
}

function vlaExecutionSummary(runtime) {
  const state = runtime.execution_readiness;
  if (!state) return "动作条件：当前接口未提供";
  if (state.checked === false && state.ready == null) return "动作条件：服务启动未检查，执行时核验";
  if (runtime.http?.policy_running === true) return "动作条件：任务执行中";
  if (state.ready === true) return "动作条件：检查通过";
  const labels = {SONIC_NOT_RUNNING_OR_AMBIGUOUS: "SONIC 未运行或实例未核清",
    SONIC_STATE_UNAVAILABLE: "SONIC 状态不可用", DIRECT_O6_NOT_RUNNING: "O6 未运行",
    O6_FEEDBACK_UNAVAILABLE: "O6 反馈不可用"};
  const reasons = (state.blockers || []).slice(0, 3).map(x => labels[x] || String(x).slice(0, 100));
  return `动作条件：${reasons.join("；") || "待核验"}`;
}

function vlaSummaryLines(item) {
  const runtime = item.health?.runtime;
  if (!runtime) return ["状态待核验"];
  const describe = (part = {}) => {
    if (part.state === "stopped") return "已停止";
    const recovering = {initializing: "初始化中", recovering: "恢复中", blocked: "恢复受阻"};
    if (recovering[part.state]) return recovering[part.state];
    if (part.state !== "running") return "未知";
    return part.ready === true ? "正常" : part.ready === false ? "未就绪" : "运行中 · 待核验";
  };
  const http = runtime.http || {};
  const camera = runtime.camera || {};
  let frames = describe(camera);
  if (camera.state !== "stopped" && Object.keys(camera.streams || {}).length) {
    frames = camera.frames_verified ? "三路正常" :
      [["ego_view", "头"], ["left_wrist", "左腕"], ["right_wrist", "右腕"]].map(([key, label]) => {
        const ready = camera.streams[key]?.ready;
        return `${label}${ready === true ? "正常" : ready === false ? "无新帧" : "未知"}`;
      }).join(" · ");
  }
  return [
    `接口：${http.draining ? "停止中" : describe(http)}${http.policy_running ? " · 任务执行中" : ""}`,
    `相机：${frames}`,
    `O6：${describe(runtime.o6)}${o6FaultSummary(runtime.o6)}`,
    ...(o6RecoverySummary(runtime.o6) ? [o6RecoverySummary(runtime.o6)] : []),
    vlaModelSummary(runtime),
    vlaExecutionSummary(runtime),
  ];
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
  el.classList.toggle("robot-card", item.layer === "robot");
  el.onclick = () => selectService(item.id);
  const port = item.port ? `端口 ${item.port}` : item.id === "gateway" ? "独立管理接口" : "无本地端口";
  const line = healthLine(item);
  el.innerHTML = `
    <div class="card-top">
      <span class="name">${escapeHtml(item.name)}</span>
      <span class="badge ${escapeHtml(item.state)}">${escapeHtml(STATE_LABEL[item.state] || item.state)}</span>
    </div>
    <div class="meta">
      ${item.id === "vla" ? `
        <div class="vla-summary">${vlaSummaryLines(item).map((text) => `<div>${escapeHtml(text)}</div>`).join("")}</div>
        <details class="vla-details">
          <summary>详情</summary>
          <div class="vla-components">
            ${item.health?.url ? `<div>${escapeHtml(item.health.url)}</div>` : ""}
            ${vlaComponentLines(item).map((text) => `<div>${escapeHtml(text)}</div>`).join("")}
            ${line ? `<div>${escapeHtml(line)}</div>` : ""}
          </div>
        </details>
      ` : `
      <div>${entry ? "任务入口 · 共用大脑" : escapeHtml(port)}</div>
      ${item.health && item.health.url ? `<div class="detail">${escapeHtml(item.health.url)}</div>` : ""}
      ${item.note ? `<div class="detail">${escapeHtml(item.note)}</div>` : ""}
      <div class="health">${escapeHtml(line)}</div>
      `}
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
    const label = item.id === "vla" && action === "stop" ? "停止 HTTP" : REMOTE_ACTION_LABEL[action] || action;
    const button = actionButton(label, () => act(item, action), action !== "start");
    if (item.id === "vla" && action === "stop") {
      button.disabled = item.health?.runtime?.http?.can_stop !== true;
      button.title = item.health?.runtime?.http?.reason || "停止前需确认 HTTP 实例及任务状态";
    }
    actions.append(button);
    hasActions = true;
  });
  if (item.review_url) {
    const button = actionButton("打开 9882", () => {
      window.open(item.review_url, "dream-review");
    }, false);
    button.disabled = !item.review_ok;
    button.title = item.review_ok
      ? item.review_url
      : "9882 当前不可达，请检查导航侧审核服务";
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
  host.textContent = `${modeText}${route?.backend === "reception_protocol" ? " · 协议模拟" : route?.backend === "reception_simple" ? " · SIMPLE 物理仿真" : ""} · ${taskText}`;
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
  if (item.id === "vla" || el.dataset.state !== item.state || el.dataset.review !== String(item.review_ok)) {
    const scrollTop = el.querySelector(".meta")?.scrollTop || 0;
    const expanded = el.querySelector(".vla-details")?.open || false;
    const replacement = card(item);
    el.replaceWith(replacement);
    const disclosure = replacement.querySelector(".vla-details");
    if (disclosure) disclosure.open = expanded;
    const details = replacement.querySelector(".meta");
    if (details) details.scrollTop = scrollTop;
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
    environment: "选择 SIMPLE 仿真或真机，应用后到任务网页开始测试。",
    robot: "VLA 启动准备相机、O6 和接口；已启用的开门模型也会提前准备。停止保留相机和 O6。导航与网关独立管理。",
  }[selectedLayer];
  document.querySelector(".workspace").classList.toggle("environment", environment);
  $("execution-settings").hidden = !environment;
  $("services-title").hidden = !environment;
  $("tabs").hidden = brain || environment;
  document.querySelector(".workspace").classList.toggle("brain", brain);
  $("layer-hint").hidden = brain;
  renderCards($("cards"), items, full);
  $("log-pin").hidden = true;
  $("log-pin").textContent = "";
  const tabs = $("tabs");
  const logs = logItems();
  const tabIds = logs.map((item) => item.id).join(",");
  if (full || tabs.dataset.ids !== tabIds) {
    tabs.replaceChildren(...logs.map(tab));
    tabs.dataset.ids = tabIds;
  }
  markActive();
  dots(snapshot.brain || [], "brain-dots");
  dots((snapshot.environment || []).filter((item) => item.id === "simple_o7"), "environment-dots");
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
        ? "已收到停止。正在关闭 NAV 工作流，保留网关和 NX SONIC …"
        : item.id === "vla" && action === "start"
          ? "已收到启动。正在准备相机、O6 和接口，并等待已启用的开门模型就绪 …"
          : item.id === "vla" && action === "stop"
            ? "已收到停止。正在确认 VLA 接口和已启用的模型服务退出；相机和 O6 保留运行 …"
            : `正在${{start: "启动", stop: "停止", restart: "重启"}[action] || action} ${item.name} …`;
  try {
    const response = await fetch(`/api/services/${item.id}/${action}`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok || payload.ok === false || item.id === "vla" && payload.confirmed !== true) {
      status.textContent = payload.error || "操作失败";
      status.classList.add("error");
    } else if (item.id === "vla") {
      status.textContent = payload.message || "启停结果已核验，请查看组件状态。";
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
  const current = currentItem();
  if (!current) return;
  if (!reset && activeLogRequest === current.id) return;
  activeLogRequest = current.id;
  const token = ++logToken;
  if (logAbort) logAbort.abort();
  logAbort = new AbortController();
  const controller = logAbort;
  const deadline = setTimeout(() => controller.abort(), 5000);
  const log = $("log");
  const stick = !reset && stickToBottom(log);
  try {
    const params = new URLSearchParams({lines: "8000"});
    const logs = await fetch(
      `/api/services/${encodeURIComponent(current.id)}/logs?${params}`,
      { signal: controller.signal, cache: "no-store" }
    );
    const body = await logs.json();
    if (!logs.ok) throw new Error(body.error || "日志读取失败");
    if (token !== logToken || body.id !== selected) return;
    log.classList.remove("error", "loading");
    const signature = JSON.stringify([current.id, body.text]);
    if (signature !== renderedLog) {
      const scroll = log.scrollTop;
      const horizontalScroll = log.scrollLeft;
      log.textContent = (body.text || "暂无日志").replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "");
      renderedLog = signature;
      if (!reset && !stick) log.scrollTop = scroll;
      log.scrollLeft = reset ? 0 : horizontalScroll;
    }
    logPath = body.path || "";
    $("log-meta").textContent = logPath;
    if (reset || stick) log.scrollTop = log.scrollHeight;
  } catch (error) {
    if (token !== logToken) return;
    log.classList.remove("loading");
    renderedLog = "";
    log.textContent = error.name === "AbortError" ? "日志读取超时，正在重试…" : String(error);
    log.classList.add("error");
  } finally {
    clearTimeout(deadline);
    if (token === logToken) activeLogRequest = "";
  }
}

async function refreshLayer(layer) {
  if (statusInFlight.has(layer)) return;
  statusInFlight.add(layer);
  try {
    const response = await fetch(`/api/status?layer=${layer}&view=panel`, {signal: AbortSignal.timeout(15000)});
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
  for (const layer of ["brain", "environment", "robot"]) {
    if (layer !== selectedLayer) refreshLayer(layer);
  }
  refreshOverview();
}

// Historical diagnostics are an explicit read, independent of live log polling.
function installDiagnosticSearch() {
  const form = $("diagnostic-form");
  if (!form) return;
  let result = null;
  let pending = false;
  let revision = 0;
  const output = $("diagnostic-results"), status = $("diagnostic-status");
  const download = $("diagnostic-export"), submit = $("diagnostic-submit");
  form.addEventListener("input", () => { revision++; result = null; download.disabled = true; });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (pending) return;
    const params = new URLSearchParams();
    for (const [key, id] of [["task_id", "diagnostic-task"], ["command_id", "diagnostic-command"],
      ["request_id", "diagnostic-request"], ["date_from", "diagnostic-from"], ["date_to", "diagnostic-to"]]) {
      const value = $(id).value.trim();
      if (value) params.set(key, value);
    }
    if (!params.size) { status.textContent = "请填写任务、命令、请求 ID 或日期范围。"; return; }
    params.set("limit", "500");
    pending = true; result = null; submit.disabled = true; download.disabled = true;
    const requestedRevision = revision;
    status.textContent = "正在只读检索历史记录…";
    output.textContent = "";
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(`/api/brain/diagnostics?${params}`, {cache: "no-store", signal: controller.signal});
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "诊断查询失败");
      if (requestedRevision !== revision) { status.textContent = "查询条件已变化，请重新查询。"; return; }
      result = body;
      const coverage = body.coverage || {};
      const missing = (body.missing_evidence || []).map(item => item.service).join("、");
      status.textContent = `找到 ${coverage.returned || 0} 条记录` +
        (coverage.incomplete ? "；扫描不完整，请查看限制。" : "；在本次扫描范围内查询完成。") +
        (missing ? ` 缺少匹配对端证据：${missing}。` : "") + " 这是历史证据，不是当前状态。";
      output.textContent = JSON.stringify({coverage, missing_evidence: body.missing_evidence,
        peer_evidence: body.peer_evidence, records: body.records, startup_sources: body.startup_sources}, null, 2);
      download.disabled = false;
    } catch (error) {
      status.textContent = error.name === "AbortError" ? "诊断查询超时，可缩小日期范围后重试。" : String(error);
    } finally {
      clearTimeout(deadline); pending = false; submit.disabled = false;
    }
  });
  download.addEventListener("click", () => {
    if (!result) return;
    // Export exactly the reviewed result; do not silently re-query changing logs.
    const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], {type: "application/json"}));
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = "brain-diagnostics.json"; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
}
function renderRobotReport(view) {
  const report = view.report || {};
  const statuses = {registered: "已登记", recording_disabled: "未启用录制", starting: "录制启动中",
    recording: "正在录制", finishing: "正在收尾", complete: "报告已生成", partial: "报告不完整", interrupted: "录制已中断"};
  const status = report.status || report.state;
  let message = status ? (statuses[status] || `报告状态：${status}`)
    : !view.enabled ? "任务诊断未启用"
      : view.tracking === "not_tracked" ? "该任务尚无诊断关联"
        : view.tracking === "not_recorded" ? "任务结束前未启动录制，未补录历史任务"
        : view.refreshing ? "正在读取网关已有摘要" : "尚未取得报告";
  if (view.refreshing) message += "；正在刷新（仅读取）";
  if (view.error) message += `；查询异常 ${view.error.code || ""}：${view.error.message || "未提供原因"}`;
  if (view.report && view.error) message += "；以下保留该任务的缓存报告";
  $("robot-report-status").textContent = message;
  const quality = report.quality || report.coverage || {};
  const missing = [...(view.incomplete_reasons || []), ...(report.missing_evidence || []), ...(quality.missing_evidence || [])];
  for (const part of ["capture", "analysis", "archive"]) {
    if (Array.isArray(quality[part]?.missing_evidence)) missing.push(...quality[part].missing_evidence.map(x => `${part}: ${x}`));
  }
  const loss = quality.loss_visibility;
  $("robot-report-summary").textContent = `任务：${view.task_id} · 录制关联：${view.run_id || "未取得"}` +
    (view.dropped_events ? ` · 未送达诊断事件：${view.dropped_events}` : "") +
    (missing.length ? ` · 缺失证据：${[...new Set(missing)].join("；")}` : "") +
    (quality.analysis?.state ? ` · 分析状态：${quality.analysis.state}` : "") +
    (loss ? ` · 丢失可见性：${loss === "unknown" ? "未知" : loss}` : "");
  const format = value => {
    const text = JSON.stringify(value, null, 2);
    return text.length > 16000 ? text.slice(0, 16000) + "\n…（展示已截断）" : text;
  };
  $("robot-report-quality").textContent = format({quality, missing_evidence: missing, limitations: report.limitations || []});
  $("robot-report-evidence").textContent = format((report.evidence_refs || []).map(ref => ({
    evidence_id: ref.evidence_id, kind: ref.kind, source: ref.source,
    availability: ref.available === true ? "对端标记可用" : ref.available === false ? "对端标记不可用" : "未知",
    reference: ref.reference,
  })));
  $("robot-report-metadata").textContent = format({task_id: view.task_id, run_id: view.run_id,
    tracking: view.tracking, cache_updated_at: view.updated_at, generated_at: report.generated_at,
    window: report.window, versions: report.versions || "未提供", read_only: true, task_authority: false});
  const findings = $("robot-report-findings");
  findings.replaceChildren();
  for (const [assessment, title] of [["confirmed", "已确认事实"], ["suspected", "疑似原因"], ["unknown", "尚未核清"]]) {
    const items = (report.findings || []).filter(item => (item.assessment || "unknown") === assessment);
    const heading = document.createElement("h4"), list = document.createElement("ul");
    heading.textContent = title;
    for (const item of items) {
      const line = document.createElement("li");
      line.textContent = `${item.code || ""} ${item.message || ""}` +
        (item.evidence_ids?.length ? `（证据：${item.evidence_ids.join("、")}）` : "");
      list.appendChild(line);
    }
    if (!items.length) { const line = document.createElement("li"); line.textContent = "暂无记录"; list.appendChild(line); }
    findings.appendChild(heading); findings.appendChild(list);
  }
}

function installRobotReport() {
  const form = $("robot-report-form");
  if (!form) return;
  let revision = 0, pending = false, controller = null;
  const task = $("robot-report-task"), query = $("robot-report-query"), refresh = $("robot-report-refresh");
  const status = $("robot-report-status");
  task.addEventListener("input", () => {
    revision++; controller?.abort(); pending = false; query.disabled = false; refresh.disabled = false;
    status.textContent = "任务 ID 已变化，请重新查询。";
    for (const id of ["robot-report-summary", "robot-report-quality", "robot-report-evidence", "robot-report-metadata"]) $(id).textContent = "";
    $("robot-report-findings").replaceChildren();
  });
  async function read(refreshRequested) {
    if (pending) return;
    const taskId = task.value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(taskId)) {
      status.textContent = "请填写完整的原任务 ID。"; return;
    }
    pending = true; query.disabled = true; refresh.disabled = true;
    const token = ++revision;
    controller = new AbortController();
    const active = controller;
    const deadline = setTimeout(() => active.abort(), 12000);
    status.textContent = refreshRequested ? "正在查询网关已有摘要…" : "正在读取该任务缓存…";
    try {
      for (let attempt = 0; attempt < 6; attempt++) {
        if (token !== revision) return;
        const params = new URLSearchParams({task_id: taskId, refresh: attempt === 0 && refreshRequested ? "1" : "0"});
        const response = await fetch(`/api/brain/task_diagnostics?${params}`, {method: "GET", cache: "no-store", signal: active.signal});
        const body = await response.json();
        if (token !== revision) return;
        if (body.task_id !== taskId || (body.report?.task_id && body.report.task_id !== taskId)) {
          throw new Error("报告身份不匹配，未展示其他任务的诊断。");
        }
        renderRobotReport(body);
        if (!body.refreshing || !response.ok) return;
        if (attempt < 5) await new Promise(resolve => setTimeout(resolve, 1000));
      }
      status.textContent += "；仍在读取，可稍后查询缓存。";
    } catch (error) {
      if (token !== revision) return;
      status.textContent = error.name === "AbortError" ? "诊断摘要读取超时，请稍后重试。" : String(error);
    } finally {
      clearTimeout(deadline);
      if (token === revision) { pending = false; query.disabled = false; refresh.disabled = false; }
    }
  }
  form.addEventListener("submit", event => { event.preventDefault(); read(false); });
  refresh.addEventListener("click", () => read(true));
}

installDiagnosticSearch();
installRobotReport();

document.querySelectorAll(".layer").forEach((button) => {
  button.onclick = () => selectLayer(button.dataset.layer);
});

$("server-address").textContent = window.location.host;
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { refresh(); loadLogs(false); }
});

render(true);
showLogPlaceholder();
loadLogs(true);
refresh();
setInterval(() => {
  if (!document.hidden) refresh();
}, 3000);
// Follow process output independently of slow health checks and service actions.
// loadLogs keeps one request in flight and discards replies for old selections.
setInterval(() => {
  if (!document.hidden) loadLogs(false);
}, 500);
