const STATE_LABEL = {
  tmux: "运行中",
  unmanaged: "未托管",
  stopped: "已停止",
  up: "通",
  down: "不通",
};

const REMOTE_ACTION_LABEL = {
  start: "启动",
  stop: "停止",
  "stand-enter": "站立 Enter",
};

let selectedLayer = "brain";
let selected = "master";
let busy = false;
let snapshot = { brain: [], robot: [] };
let logToken = 0;
let logAbort = null;
let statusInFlight = false;
let logPath = "";
let logKind = "brain";
let reviewWasReady = null;
let reviewOpened = false;

function $(id) {
  return document.getElementById(id);
}

function layerItems() {
  return snapshot[selectedLayer] || [];
}

function currentItem() {
  return layerItems().find((item) => item.id === selected) || layerItems()[0];
}

function selectLayer(layer) {
  selectedLayer = layer;
  const items = layerItems();
  if (!items.some((item) => item.id === selected)) {
    selected = items[0] ? items[0].id : "";
  }
  logPath = "";
  document.querySelectorAll(".layer").forEach((button) => {
    button.classList.toggle("active", button.dataset.layer === layer);
  });
  render(true);
  showLogPlaceholder();
  updateLogKinds();
  loadLogs(true);
}

function selectService(id) {
  if (selected === id) {
    loadLogs(true);
    return;
  }
  selected = id;
  logPath = "";
  markActive();
  showLogPlaceholder();
  updateLogKinds();
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
  const log = $("log");
  if (!opened) {
    log.textContent =
      `9882 审核页已就绪：${dream.review_url}\n` +
      "浏览器拦住了自动弹窗，点卡片上的「打开 9882」。";
  }
}

function card(item) {
  const el = document.createElement("article");
  el.className = "card" + (item.id === selected ? " active" : "");
  el.dataset.id = item.id;
  el.onclick = () => selectService(item.id);
  const port = item.port ? `端口 ${item.port}` : "无本地端口";
  const line = healthLine(item);
  el.innerHTML = `
    <div class="card-top">
      <span class="name">${escapeHtml(item.name)}</span>
      <span class="badge ${escapeHtml(item.state)}">${escapeHtml(STATE_LABEL[item.state] || item.state)}</span>
    </div>
    <div class="meta">
      <div>${escapeHtml(port)}</div>
      ${item.health && item.health.url ? `<div class="detail">${escapeHtml(item.health.url)}</div>` : ""}
      ${item.note ? `<div class="detail">${escapeHtml(item.note)}</div>` : ""}
      <div class="health">${escapeHtml(line)}</div>
    </div>
  `;
  const actions = document.createElement("div");
  actions.className = "actions";
  let hasActions = false;
  if (item.controllable) {
    actions.append(
      actionButton("重启", () => act(item, "restart"), item.confirm_restart),
      actionButton("停止", () => act(item, "stop"), true)
    );
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
  return el;
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
  const titles = {
    brain: "指挥日志",
    raw: "原始日志",
    http: "HTTP 访问日志",
  };
  if (current && current.id === "master") {
    $("log-title").textContent = `Master ${titles[logKind] || "指挥日志"}`;
  } else {
    $("log-title").textContent = current ? `${current.name} 运行日志` : "运行日志";
  }
  $("log-meta").textContent = logPath;
}

function updateLogKinds() {
  const host = $("log-kinds");
  const current = currentItem();
  const show = Boolean(current && current.id === "master");
  host.hidden = !show;
  host.querySelectorAll("button").forEach((button) => {
    button.classList.toggle("active", button.dataset.kind === logKind);
  });
}

function showLogPlaceholder() {
  const log = $("log");
  log.classList.remove("error");
  log.classList.add("loading");
  log.textContent = "加载中…";
}

function patchCard(el, item) {
  el.classList.toggle("active", item.id === selected);
  const badge = el.querySelector(".badge");
  if (badge) {
    badge.className = `badge ${item.state}`;
    badge.textContent = STATE_LABEL[item.state] || item.state;
  }
  const health = el.querySelector(".health");
  if (health) health.textContent = healthLine(item);
}

function render(full) {
  const items = layerItems();
  $("layer-title").textContent = selectedLayer === "brain" ? "大脑层" : "真机层";
  $("layer-count").textContent = `${items.length} 项`;
  const cards = $("cards");
  const tabs = $("tabs");
  const ids = items.map((item) => item.id).join(",");
  const needFull =
    full ||
    cards.dataset.layer !== selectedLayer ||
    cards.dataset.ids !== ids ||
    cards.childElementCount !== items.length;
  if (needFull) {
    cards.innerHTML = "";
    tabs.innerHTML = "";
    items.forEach((item) => {
      cards.appendChild(card(item));
      tabs.appendChild(tab(item));
    });
    cards.dataset.layer = selectedLayer;
    cards.dataset.ids = ids;
  } else {
    items.forEach((item) => {
      const el = cards.querySelector(`[data-id="${item.id}"]`);
      if (el) patchCard(el, item);
    });
    tabs.querySelectorAll(".tab").forEach((el) => {
      el.classList.toggle("active", el.dataset.id === selected);
    });
  }
  markActive();
  dots(snapshot.brain || [], "brain-dots");
  dots(snapshot.robot || [], "robot-dots");
}

function stickToBottom(node) {
  return node.scrollHeight - node.scrollTop - node.clientHeight < 48;
}

async function act(item, action) {
  if (busy) {
    $("log").textContent = "上一次真机操作还在进行，请稍候。";
    $("log").classList.remove("loading");
    return;
  }
  if (action === "restart" && item.confirm_restart) {
    const ok = window.confirm(
      "重启 Master 会清空 Redis 协作库（collaborator.clear=true）。确定继续？"
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
  selected = item.id;
  markActive();
  const log = $("log");
  log.classList.remove("error", "loading");
  log.textContent =
    item.id === "dream" && action === "start"
      ? "已收到启动。脚本会跑在导航机 tmux g1_panel_oneclick 里：preflight → READY → SONIC / DREAM / VLA HTTP。出现两次 Enter 提示后必须 10 分钟内按完，否则脚本会锁定导航退出。"
      : item.id === "dream" && action === "stand-enter"
        ? "已向导航机 adapter 窗口发 1 个 Enter，等它回显 …"
      : item.id === "dream" && action === "stop"
        ? "已收到停止。正在关闭 DREAM 与 VLA HTTP/relay，保留 NX SONIC …"
        : action === "start"
          ? "已收到启动。正在按文档连接 4090：check → 依赖检查 → HTTP 8091 + 导航 relay …"
          : action === "stop"
            ? "已收到停止。正在 SSH 4090 关闭 HTTP 与导航 relay …"
            : `正在执行 ${action} …`;
  try {
    const response = await fetch(`/api/services/${item.id}/${action}`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      log.textContent = payload.error || "操作失败";
      log.classList.add("error");
    }
  } catch (error) {
    log.textContent = String(error);
    log.classList.add("error");
  } finally {
    busy = false;
    refresh(true);
  }
}

async function loadLogs(reset) {
  if (busy) return;
  const current = currentItem();
  if (!current) return;
  const token = ++logToken;
  if (logAbort) logAbort.abort();
  logAbort = new AbortController();
  const log = $("log");
  const stick = !reset && stickToBottom(log);
  try {
    const kindQuery = current.id === "master" ? `&kind=${encodeURIComponent(logKind)}` : "";
    const logs = await fetch(
      `/api/services/${encodeURIComponent(current.id)}/logs?lines=3000${kindQuery}`,
      { signal: logAbort.signal }
    );
    const body = await logs.json();
    if (busy || token !== logToken || body.id !== selected) return;
    log.classList.remove("error", "loading");
    log.textContent = body.text || "暂无日志";
    logPath = body.path || "";
    $("log-meta").textContent = logPath;
    const pinBox = $("log-pin");
    if (pinBox) {
      const pinText = current.id === "master" && logKind === "brain" ? body.pin : "";
      pinBox.hidden = !pinText;
      pinBox.textContent = pinText ? `失败钉住：${pinText}` : "";
    }
    if (reset || stick) log.scrollTop = log.scrollHeight;
  } catch (error) {
    if (error.name === "AbortError" || token !== logToken || busy) return;
    log.classList.remove("loading");
    log.textContent = String(error);
    log.classList.add("error");
  }
}

async function refresh(forceCards) {
  if (statusInFlight) return;
  statusInFlight = true;
  try {
    const response = await fetch("/api/status");
    const data = await response.json();
    snapshot = data;
    popReviewPage(data.robot || []);
    $("attach").textContent = data.attach || "tmux ls";
    if (!layerItems().some((item) => item.id === selected)) {
      selected = layerItems()[0] ? layerItems()[0].id : "";
    }
    render(Boolean(forceCards));
    updateLogKinds();
    await loadLogs(false);
  } catch (error) {
    $("log-meta").textContent = String(error);
  } finally {
    statusInFlight = false;
  }
}

document.querySelectorAll(".layer").forEach((button) => {
  button.onclick = () => selectLayer(button.dataset.layer);
});

document.querySelectorAll("#log-kinds button").forEach((button) => {
  button.onclick = () => {
    logKind = button.dataset.kind || "brain";
    updateLogKinds();
    markActive();
    loadLogs(true);
  };
});

refresh(true);
setInterval(() => refresh(false), 3000);
