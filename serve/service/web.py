"""
web.py - 仿真器控制 Web UI
用法: python web.py
访问: http://localhost:8080
"""

import os
import sys
from pathlib import Path

from flask import Flask

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from robot_api.config import load_robot_api_config

app = Flask(__name__)
API_URL = os.getenv("ROBOT_API_URL", load_robot_api_config().server_url)


@app.route("/")
def index():
    return HTML_PAGE


HTML_PAGE = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>RoboCasa 仿真控制</title>
<style>
  body {{ font-family: monospace; background: #1a1a2e; color: #eee; margin: 20px; }}
  h1 {{ color: #0f3460; background: #e94560; padding: 10px 20px; border-radius: 6px; display: inline-block; }}
  .section {{ background: #16213e; padding: 15px; border-radius: 8px; margin: 10px 0; }}
  button {{ background: #0f3460; color: #eee; border: none; padding: 8px 16px;
            border-radius: 4px; cursor: pointer; margin: 4px; font-family: monospace; }}
  button:hover {{ background: #e94560; }}
  input {{ background: #1a1a2e; color: #eee; border: 1px solid #0f3460;
           padding: 6px 10px; border-radius: 4px; font-family: monospace; width: 60px; }}
  #log {{ background: #0a0a1a; color: #0f0; padding: 10px; border-radius: 6px;
          height: 300px; overflow-y: auto; white-space: pre-wrap; font-size: 13px; }}
  .obj-table {{ width: 100%; border-collapse: collapse; }}
  .obj-table td, .obj-table th {{ padding: 4px 8px; text-align: left; border-bottom: 1px solid #0f3460; }}
  .status-ok {{ color: #0f0; }}
  .status-err {{ color: #e94560; }}
</style>
</head>
<body>

<h1>RoboCasa 仿真控制</h1>

<div class="section">
  <b>物体列表</b> <button onclick="refreshObjects()">刷新</button>
  <table class="obj-table" id="obj-table">
    <tr><th>名称</th><th>位置 [x, y, z]</th><th>已抓取</th></tr>
  </table>
</div>

<div class="section">
  <b>抓取 & 放置</b><br>
  物体: <select id="grasp-obj"></select>
  吸附阈值: <input id="snap-th" value="0.2" style="width:60px"> m
  <button onclick="doGrasp()">抓取</button>
  <button onclick="doPlace()">放置(配置文件)</button>
  <button onclick="doPlaceCustom()">放置(自定义坐标)</button><br>
  目标 X: <input id="pl-x" value="0">
  Y: <input id="pl-y" value="0">
  Z: <input id="pl-z" value="0.5">
</div>

<div class="section">
  <b>移动末端</b> <button onclick="refreshStatus()" style="font-size:11px;padding:3px 8px">刷新位置</button><br>
  当前位置: <span id="ee-pos" style="color:#0f0">-</span><br>
  X: <input id="mv-x" value="0">
  Y: <input id="mv-y" value="0">
  Z: <input id="mv-z" value="0">
  <button onclick="doMoveTo()">移动</button>
</div>

<div class="section">
  <b>移动底座</b> <button onclick="refreshStatus()" style="font-size:11px;padding:3px 8px">刷新位置</button><br>
  当前位置: <span id="base-pos" style="color:#0f0">-</span>
  &nbsp; 朝向: <span id="base-yaw" style="color:#0f0">-</span><br>
  X: <input id="mb-x" value="0">
  Y: <input id="mb-y" value="0">
  Yaw(度): <input id="mb-yaw" value="0" style="width:50px">
  <button onclick="doMoveBase()">移动底座</button>
</div>

<div class="section">
  <b>夹爪</b>
  <button onclick="doOpenGripper()">打开</button>
  <button onclick="doCloseGripper()">关闭</button>
  <button onclick="doStatus()">查询状态</button>
</div>

<div class="section">
  <b>日志</b> <button onclick="document.getElementById('log').innerHTML=''">清空</button>
  <div id="log"></div>
</div>

<script>
const API = '{API_URL}';

function log(msg, ok) {{
  const el = document.getElementById('log');
  const cls = ok === false ? 'status-err' : 'status-ok';
  const time = new Date().toLocaleTimeString();
  el.innerHTML += `<span class="${{cls}}">[${{time}}] ${{msg}}</span>\\n`;
  el.scrollTop = el.scrollHeight;
}}

async function api(path, method, body) {{
  try {{
    const opts = {{method, headers: {{'Content-Type': 'application/json'}}}};
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(API + path, opts);
    const data = await res.json();
    log(`${{method}} ${{path}} → ${{JSON.stringify(data)}}`, res.ok);
    return data;
  }} catch(e) {{
    log(`请求失败: ${{e.message}}`, false);
  }}
}}

async function refreshObjects() {{
  const data = await api('/objects', 'GET');
  if (!data) return;
  const table = document.getElementById('obj-table');
  table.innerHTML = '<tr><th>名称</th><th>位置</th><th>已抓取</th></tr>';
  // 更新物体下拉框
  const sel = document.getElementById('grasp-obj');
  const prev = sel.value;
  sel.innerHTML = '';
  for (const [name, info] of Object.entries(data)) {{
    const pos = info.pos.map(v => v.toFixed(3)).join(', ');
    const grasped = info.grasped ? '✓' : '';
    table.innerHTML += `<tr><td>${{name}}</td><td>[${{pos}}]</td><td>${{grasped}}</td></tr>`;
    const opt = document.createElement('option');
    opt.value = name; opt.textContent = name;
    sel.appendChild(opt);
  }}
  if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
}}

async function doGrasp() {{
  const obj = document.getElementById('grasp-obj').value;
  const th = parseFloat(document.getElementById('snap-th').value);
  await api('/grasp', 'POST', {{obj_name: obj, snap_threshold: th}});
  refreshStatus();
  refreshObjects();
}}

async function doPlace() {{
  const obj = document.getElementById('grasp-obj').value;
  const x = parseFloat(document.getElementById('pl-x').value);
  const y = parseFloat(document.getElementById('pl-y').value);
  const z = parseFloat(document.getElementById('pl-z').value);
  await api('/place', 'POST', {{obj_name: obj, target: [x, y, z]}});
  refreshStatus();
  refreshObjects();
}}

async function doPlaceCustom() {{
  const obj = document.getElementById('grasp-obj').value;
  const x = parseFloat(document.getElementById('pl-x').value);
  const y = parseFloat(document.getElementById('pl-y').value);
  const z = parseFloat(document.getElementById('pl-z').value);
  await api('/place', 'POST', {{obj_name: obj, target: [x, y, z]}});
  refreshStatus();
  refreshObjects();
}}

async function doMoveTo() {{
  const x = parseFloat(document.getElementById('mv-x').value);
  const y = parseFloat(document.getElementById('mv-y').value);
  const z = parseFloat(document.getElementById('mv-z').value);
  await api('/move_to', 'POST', {{target: [x, y, z]}});
  refreshStatus();
}}

async function doMoveBase() {{
  const x = parseFloat(document.getElementById('mb-x').value);
  const y = parseFloat(document.getElementById('mb-y').value);
  const yaw = parseFloat(document.getElementById('mb-yaw').value);
  // 先获取当前 yaw
  const baseRes = await fetch(API + '/base_status');
  const baseData = await baseRes.json();
  const currentYaw = baseData.yaw_deg || 0;
  await api('/nav', 'POST', {{x: x, y: y, w: yaw, yaw: currentYaw}});
  refreshStatus();
}}

function doOpenGripper() {{ api('/open_gripper', 'POST', {{}}); }}
function doCloseGripper() {{ api('/close_gripper', 'POST', {{}}); }}
function doStatus() {{ api('/status', 'GET'); }}

async function refreshStatus() {{
  try {{
    // 获取手臂状态
    const res = await fetch(API + '/status');
    const data = await res.json();
    if (!data.error && data.ee_pos) {{
      document.getElementById('ee-pos').textContent =
        '[' + data.ee_pos.map(v => v.toFixed(3)).join(', ') + ']';
    }}
  }} catch(e) {{}}

  try {{
    // 获取底座状态
    const res2 = await fetch(API + '/base_status');
    const data2 = await res2.json();
    if (!data2.error && data2.pos) {{
      document.getElementById('base-pos').textContent =
        '[' + data2.pos.map(v => v.toFixed(3)).join(', ') + ']';
      document.getElementById('base-yaw').textContent = data2.yaw_deg + '°';
    }}
  }} catch(e) {{
    log('状态查询失败: ' + e.message, false);
  }}
}}

// 启动时刷新 + 每2秒自动刷新
refreshObjects();
refreshStatus();
setInterval(refreshStatus, 2000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("Web UI 启动: http://localhost:8080")
    app.run(host="0.0.0.0", port=8080, debug=False)
