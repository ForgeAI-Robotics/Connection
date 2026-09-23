(() => {
  const el = (id) => document.getElementById(id);
  const names = {reception: '接待', execution: '通用执行 / 桌面整理', observation: '现场观察 / 网页画面'};
  let loaded = false;
  let submitting = false;
  for (const [key, label] of Object.entries(names)) {
    const row = document.createElement('label');
    row.textContent = label + ' ';
    const mode = document.createElement('select');
    mode.id = 'exec-' + key;
    for (const [value, text] of Object.entries({inherit: '跟随全局', simulation: '仿真', real: '真机', disabled: '停用'})) {
      mode.add(new Option(text, value));
    }
    row.append(mode);
    if (key !== 'reception') {
      const backend = document.createElement('select');
      backend.id = 'exec-' + key + '-sim';
      for (const [value, text] of Object.entries({inherit: '跟随默认仿真器', desk: 'Desk（无画面）', mujoco: 'MuJoCo', mujoco_3dgs: '3DGS'})) {
        backend.add(new Option(text, value));
      }
      row.append(backend);
    }
    el('exec-modules').append(row);
  }
  function config() {
    const modules = {};
    for (const key of Object.keys(names)) {
      modules[key] = {mode: el('exec-' + key).value};
      if (key !== 'reception') modules[key].simulation_backend = el('exec-' + key + '-sim').value;
    }
    return {mode: el('exec-mode').value, simulation_backend: el('exec-sim').value, modules};
  }
  function fill(c) {
    el('exec-mode').value = c.mode || 'simulation';
    el('exec-sim').value = c.simulation_backend || 'desk';
    for (const key of Object.keys(names)) {
      el('exec-' + key).value = c.modules?.[key]?.mode || 'inherit';
      if (key !== 'reception') el('exec-' + key + '-sim').value = c.modules?.[key]?.simulation_backend || 'inherit';
    }
  }
  function describe(profile) {
    return Object.entries(profile.routes).map(([key, r]) =>
      `${names[key]}：${r.mode === 'real' ? '真机' : r.mode === 'simulation' ? '仿真' : '停用'} / ${r.backend}` +
      (r.available ? (r.url ? `（${r.url}）` : '') : ` — 不可用：${r.reason}`)).join('\n');
  }
  async function post(action) {
    const response = await fetch('/api/execution/' + action, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(config())
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '请求失败');
    return data;
  }
  async function refresh() {
    try {
      const r = await fetch('/api/execution');
      const data = await r.json();
      if (!r.ok) throw new Error(data.error);
      if (!loaded) { fill(data.draft); loaded = true; }
      const busy = data.applying || submitting;
      for (const button of document.querySelectorAll('.execution-settings button')) button.disabled = busy;
      el('exec-status').textContent = (data.applying ? '正在应用，请稍候…\n' : '') +
        (data.error ? `应用失败：${data.error}\n` : '') +
        (data.blocked ? '环境未恢复，已阻止新任务。重新应用成功后解除。\n' : '') +
        (data.applied ? '已生效：\n' + describe(data.applied) : '尚未启用统一配置；当前沿用原配置。') +
        '\n真机动作许可：' + (data.real_motion_enabled ? '已开启（仍需通过下游闸门）' : '关闭');
    } catch (e) { el('exec-status').textContent = e.message; }
  }
  async function apply() {
    if (submitting) return;
    submitting = true;
    try {
      await post('apply');
      el('exec-status').textContent = '已提交，正在检查任务并应用环境…';
    } catch (e) { el('exec-status').textContent = e.message; }
    finally { submitting = false; }
  }
  el('exec-preview').onclick = async () => {
    el('exec-preview-text').hidden = false;
    try { el('exec-preview-text').textContent = '待应用：\n' + describe(await post('preview')); }
    catch (e) { el('exec-preview-text').textContent = e.message; }
  };
  el('exec-apply').onclick = apply;
  for (const [id, mode] of [['exec-all-sim', 'simulation'], ['exec-all-real', 'real']]) {
    el(id).onclick = () => { fill({mode, simulation_backend: el('exec-sim').value, modules: {}}); apply(); };
  }
  refresh();
  setInterval(refresh, 3000);
})();
