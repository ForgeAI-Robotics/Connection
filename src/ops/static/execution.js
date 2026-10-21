(() => {
  const el = (id) => document.getElementById(id);
  const choices = [...document.querySelectorAll('input[name="exec-mode"]')];
  const labels = {simulation: 'SIMPLE 仿真', real: '真机'};
  const modules = {reception: '接待', execution: '通用执行', observation: '现场观察 / 网页画面'};
  const backends = {reception_real: '真机', reception_simple: 'SIMPLE 仿真', simple_o7: 'SIMPLE 仿真'};
  let state = null;
  let dirty = false;
  let submitting = false;
  let refreshing = false;
  let generation = 0;
  let lastNotice = '';

  function selectedMode() { return choices.find((choice) => choice.checked)?.value; }

  // Use the existing configuration contract and switch transaction. Explicitly
  // select reception_simple: inheriting the simulation default selects the old demo.
  function config() {
    const mode = selectedMode();
    if (!labels[mode]) throw new Error('请先选择 SIMPLE 仿真或真机');
    return {mode, simulation_backend: 'simple_o7', modules: {
      reception: {mode: 'inherit', simulation_backend: 'reception_simple'},
      execution: {mode: 'inherit', simulation_backend: 'inherit'},
      observation: {mode: 'inherit', simulation_backend: 'inherit'}
    }};
  }

  function appliedMode(profile) {
    const routes = profile?.routes;
    if (!routes) return null;
    const all = Object.keys(modules).map((key) => routes[key]);
    if (all.every((route) => route?.mode === 'real') && routes.reception.backend === 'reception_real') return 'real';
    if (all.every((route) => route?.mode === 'simulation') &&
        routes.reception.backend === 'reception_simple' &&
        routes.execution.backend === 'simple_o7' && routes.observation.backend === 'simple_o7') return 'simulation';
    return null;
  }

  function controls() {
    const busy = submitting || state?.applying;
    el('exec-choices').disabled = !state || busy;
    el('exec-apply').disabled = !state || busy || !selectedMode();
    el('exec-apply').textContent = busy ? '应用中…' : '应用';
    const mode = selectedMode();
    el('exec-selection').hidden = Boolean(mode && mode === appliedMode(state?.applied));
    el('exec-selection').textContent = mode ? '待应用' : '请选择环境';
  }

  function describe(profile) {
    return Object.entries(profile?.routes || {}).map(([key, route]) => {
      const target = backends[route.backend] || route.backend;
      const urls = route.endpoints ? Object.values(route.endpoints).join('、') : route.url;
      return `${modules[key] || key}：${target}` +
        (route.available ? (urls ? `（${urls}）` : '') : ` — 不可用：${route.reason}`);
    }).join('\n');
  }

  function feedback(text, error = false) {
    el('exec-feedback').textContent = text;
    el('exec-feedback').hidden = !text;
    el('exec-feedback').classList.toggle('error', error);
  }

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    const requestedGeneration = generation;
    try {
      const response = await fetch('/api/execution', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
      const data = await response.json();
      if (requestedGeneration !== generation) return;
      if (!response.ok) throw new Error(data.error || '环境读取失败');
      state = data;
      const mode = appliedMode(data.applied);
      if (!dirty && !submitting) {
        for (const choice of choices) choice.checked = choice.value === mode;
      }
      const reception = data.applied?.routes?.reception;
      const current = mode ? labels[mode] : data.applied
        ? `其他配置（接待：${backends[reception?.backend] || reception?.backend || '未配置'}）`
        : '未确认，请选择环境并应用';
      el('exec-status').textContent = `当前已生效：${current}` +
        (data.blocked ? ' · 新任务已阻断' : '') +
        (data.applying ? ' · 正在切换，等待完成' : '');
      el('exec-details').textContent = describe(data.applied) +
        '\n真机动作许可：' + (data.real_motion_enabled ? '已开启，仍需通过下游闸门' : '关闭') +
        '\n真机环境可在任务页选择「导航真机＋操控模拟」进行联调。';
      const failure = data.error || (data.last_apply?.state === 'failed' ? data.last_apply.error : '');
      const notice = JSON.stringify([data.applying, data.blocked, failure, data.last_apply, data.applied?.revision]);
      if (notice !== lastNotice) {
        lastNotice = notice;
        if (data.applying) feedback('正在检查任务并应用环境，请稍候…');
        else if (failure) feedback(`上次应用失败：${failure}` +
          (data.blocked ? '\n环境尚未恢复，请排查后重新应用。' : '\n当前环境以已生效状态为准。'), true);
        else if (data.blocked) feedback('环境尚未恢复，新任务已阻断，请排查后重新应用。', true);
        else feedback('');
      }
      controls();
    } catch (error) {
      if (requestedGeneration !== generation) return;
      state = null;
      lastNotice = '';
      el('exec-status').textContent = '当前生效环境暂时无法确认';
      feedback(error.message, true);
      controls();
    } finally { refreshing = false; }
  }

  for (const choice of choices) choice.onchange = () => { dirty = true; controls(); };
  el('exec-apply').onclick = async () => {
    if (submitting || state?.applying) return;
    const payload = config();
    generation++;
    submitting = true;
    controls();
    feedback('正在提交环境切换…');
    try {
      const response = await fetch('/api/execution/apply', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload), signal: AbortSignal.timeout(10000)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '提交失败');
      dirty = false;
      lastNotice = '';
      // Keep controls locked until a subsequent status read confirms completion.
      if (state) state.applying = true;
      feedback('已提交，正在检查任务并应用环境…');
    } catch (error) {
      feedback(`切换提交未确认：${error.message}。请核对最新生效状态。`, true);
    } finally {
      submitting = false;
      controls();
    }
    await refresh();
  };
  refresh();
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  setInterval(() => { if (!document.hidden) refresh(); }, 3000);
})();
