const STATE_LABEL = {
  tmux: "运行中",
  unmanaged: "未托管",
  stopped: "已停止",
  up: "通",
  down: "不通",
};

let selectedLayer = "brain";
let selected = "master";
let busy = false;
let snapshot = { brain: [], robot: [] };
let logToken = 0;
let logAbort = null;
let statusInFlight = false;

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
  document.querySelectorAll(".layer").forEach((button) => {
    button.classList.toggle("active", button.dataset.layer === layer);
  });
  render(true);
  showLogPlaceholder();
  loadLogs(true);
}

function selectService(id) {
  if (selected === id) {
    loadLogs(true);
    return;
  }
  selected = id;
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
  const health = item.health || {};
  const latency = health.latency_ms != null ? `${health.latency_ms}ms` : "";
  return [health.detail, latency].filter(Boolean).join(" · ");
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
      ${item.attach ? `<div class="detail"><code>${escapeHtml(item.attach)}</code></div>` : ""}
      ${item.health && item.health.url ? `<div class="detail">${escapeHtml(item.health.url)}</div>` : ""}
      <div class="health">${escapeHtml(line)}</div>
    </div>
  `;
  if (item.controllable) {
    const actions = document.createElement("div");
    actions.className = "actions";
    actions.append(
      actionButton("重启", () => act(item, "restart"), item.confirm_restart),
      actionButton("停止", () => act(item, "stop"), true)
    );
    el.appendChild(actions);
  }
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
  $("log-title").textContent = current ? `${current.name} 运行日志` : "运行日志";
  $("log-meta").textContent = current && current.attach ? current.attach : "";
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
  if (busy) return;
  if (action === "restart" && item.confirm_restart) {
    const ok = window.confirm(
      "重启 Master 会清空 Redis 协作库（collaborator.clear=true）。确定继续？"
    );
    if (!ok) return;
  }
  busy = true;
  try {
    const response = await fetch(`/api/services/${item.id}/${action}`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok && selected === item.id) {
      $("log").textContent = payload.error || "操作失败";
      $("log").classList.add("error");
    }
  } catch (error) {
    if (selected === item.id) {
      $("log").textContent = String(error);
      $("log").classList.add("error");
    }
  } finally {
    busy = false;
    refresh(true);
  }
}

async function loadLogs(reset) {
  const current = currentItem();
  if (!current) return;
  const token = ++logToken;
  if (logAbort) logAbort.abort();
  logAbort = new AbortController();
  const log = $("log");
  const pin = !reset && stickToBottom(log);
  try {
    const logs = await fetch(`/api/services/${encodeURIComponent(current.id)}/logs?lines=250`, {
      signal: logAbort.signal,
    });
    const body = await logs.json();
    if (token !== logToken || body.id !== selected) return;
    log.classList.remove("error", "loading");
    log.textContent = body.text || "暂无日志";
    if (reset || pin) log.scrollTop = log.scrollHeight;
  } catch (error) {
    if (error.name === "AbortError" || token !== logToken) return;
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
    $("attach").textContent = data.attach || "tmux ls";
    if (!layerItems().some((item) => item.id === selected)) {
      selected = layerItems()[0] ? layerItems()[0].id : "";
    }
    render(Boolean(forceCards));
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

refresh(true);
setInterval(() => refresh(false), 3000);
