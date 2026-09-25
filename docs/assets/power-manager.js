'use strict';
(() => {
 const el = id => document.getElementById(id);
 let reading = false, applying = false, available = false, initialized = false;
 function show(state) {
  available = state.available === true;
  el('power-apply').disabled = applying || reading || !available;
  if (!available) {
   el('power-state').textContent = '实际功率暂时无法确认：' + (state.error || '请稍后刷新');
   return;
  }
  const limits = state.limits;
  el('power-state').textContent = `当前生效：${state.label} · 短时上限 ${limits['fast-limit']}W / 持续上限 ${limits['slow-limit']}W · 核对时间 ${new Date(state.checked_at * 1000).toLocaleTimeString('zh-CN')}`;
  if (!initialized && state.profile) { el('power-profile').value = state.profile; initialized = true; }
 }
 async function refreshPower() {
  if (reading || applying) return;
  reading = true;
  el('power-apply').disabled = true;
  try {
   const response = await fetch('/api/power', {cache: 'no-store', signal: AbortSignal.timeout(45000)});
   const state = await response.json();
   if (!response.ok || !state.available) throw new Error(state.error || `HTTP ${response.status}`);
   show(state);
  } catch (error) { show({available: false, error: error.message}); }
  finally { reading = false; el('power-apply').disabled = applying || !available; }
 }
 el('power-refresh').onclick = refreshPower;
 el('power-profile').onchange = () => { initialized = true; };
 el('power-form').onsubmit = async event => {
  event.preventDefault();
  if (applying || reading || !available) return;
  applying = true;
  el('power-apply').disabled = true;
  el('power-profile').disabled = true;
  el('power-result').textContent = '正在切换并核对实际功率…';
  try {
   const response = await fetch('/api/control', {method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Dashboard-Request': '1', 'Authorization': 'Bearer ' + el('power-token').value},
    body: JSON.stringify({action: 'set_power', profile: el('power-profile').value}), signal: AbortSignal.timeout(60000)});
   const result = await response.json();
   if (!response.ok || result.error) throw new Error(result.error || `HTTP ${response.status}`);
   show(result.power);
   el('power-result').textContent = `已生效：${result.power.label}。实际限制已核对，任务可继续运行。`;
  } catch (error) { el('power-result').textContent = '未能确认切换完成：' + error.message; }
  finally {
   el('power-token').value = '';
   applying = false;
   el('power-profile').disabled = false;
   el('power-apply').disabled = !available;
   await refreshPower();
  }
 };
 document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshPower(); });
 refreshPower();
 setInterval(() => { if (!document.hidden) refreshPower(); }, 15000);
})();
