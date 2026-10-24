'use strict';
const $ = id => document.getElementById(id);
let csrf = '', revision = 0, dirty = false;
const gib = bytes => (bytes / 1024 ** 3).toFixed(3) + ' GiB';
function message(text, error = false) { $('message').textContent = text; $('message').classList.toggle('error', error); }
function signedIn(yes) { $('login-section').hidden = yes; $('dashboard').hidden = !yes; $('logout').hidden = !yes; }
async function api(path, body) {
  const options = { credentials: 'same-origin', headers: {} };
  if (body !== undefined) { options.method = 'POST'; options.headers = { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }; options.body = JSON.stringify(body); }
  const r = await fetch('/admin/api/' + path, options), data = await r.json();
  if (!r.ok) { if (r.status === 401) signedIn(false); throw Error(data.error || '请求失败'); }
  if (data.csrf) csrf = data.csrf;
  return data;
}
function rows(target, data, first) {
  $(target).replaceChildren();
  for (const row of data) { const tr = document.createElement('tr'); for (const text of [row[first], gib(row.bytes)]) { const td = document.createElement('td'); td.textContent = text; tr.append(td); } $(target).append(tr); }
  if (!data.length) { const tr = document.createElement('tr'), td = document.createElement('td'); td.colSpan = 2; td.textContent = '暂无下载记录'; tr.append(td); $(target).append(tr); }
}
async function refresh() {
  const d = await api('status'); signedIn(true); revision = d.revision;
  $('total-used').textContent = gib(d.total_used); $('budget').textContent = '每日上限 ' + gib(d.settings.limits.daily_bytes);
  $('active').textContent = d.active_downloads; $('visitors').textContent = d.visitors;
  $('memory').textContent = d.memory_bytes === null ? '—' : (d.memory_bytes / 1024 ** 2).toFixed(1) + ' MiB';
  $('uptime').textContent = '已运行 ' + Math.floor(d.uptime_seconds / 60) + ' 分钟';
  $('service-state').textContent = d.settings.paused ? '已暂停新下载' : d.settings.public ? '公开下载运行中' : '私用下载运行中';
  const s = d.settings, l = s.limits;
  $('public').checked = s.public; $('paused').checked = s.paused; $('ip-concurrency').value = l.ip_concurrency;
  $('speed').value = l.bytes_per_second / 1024 ** 2; $('ip-daily').value = l.ip_daily_bytes / 1024 ** 3;
  $('daily').value = l.daily_bytes / 1024 ** 3; $('requests').value = l.requests_per_minute; $('domains').value = s.allowed_domains.join('\n');
  $('proxy-status').textContent = d.verified_proxy_requests ? `本次启动已验证 ${d.verified_proxy_requests} 次可信代理请求。` : '尚未确认可信代理请求；CDN 访客可能正在共享节点 IP 限额。';
  $('quota-note').textContent = `统计日期 ${d.day}，时区 ${d.timezone}。下载开始时预留额度，未发送部分在结束时退还；统计包含在途预留，不等于 CDN 账单。`;
  rows('history', d.history, 'day'); rows('clients', d.clients, 'visitor'); $('version').textContent = '番星 · 星桥 ' + d.version;
  $('audit').replaceChildren(); for (const entry of d.audit) { const li = document.createElement('li'); li.textContent = entry.at + ' · ' + entry.action; $('audit').append(li); }
  if (!d.audit.length) { const li = document.createElement('li'); li.textContent = '暂无管理操作'; $('audit').append(li); }
  dirty = false;
}
async function busy(form, fn) { const button = form.querySelector('button'); button.disabled = true; try { await fn(); } catch (e) { message(e.message, true); } finally { button.disabled = false; } }
$('login-form').addEventListener('submit', e => { e.preventDefault(); busy(e.currentTarget, async () => { await api('login', { username: $('username').value, password: $('password').value }); $('password').value = ''; await refresh(); message('登录成功。'); }); });
$('settings-form').addEventListener('input', () => { dirty = true; });
$('settings-form').addEventListener('submit', e => { e.preventDefault(); busy(e.currentTarget, async () => {
  const settings = { public: $('public').checked, paused: $('paused').checked,
    limits: { ip_concurrency: Number($('ip-concurrency').value), bytes_per_second: Math.round(Number($('speed').value) * 1024 ** 2), ip_daily_bytes: Math.round(Number($('ip-daily').value) * 1024 ** 3), daily_bytes: Math.round(Number($('daily').value) * 1024 ** 3), requests_per_minute: Number($('requests').value) },
    allowed_domains: $('domains').value.split(/\s+/).filter(Boolean) };
  await api('settings', { settings, revision }); await refresh(); message('下载规则已保存并立即生效，现有流量记录保留。');
}); });
$('refresh').addEventListener('click', async () => { if (dirty && !confirm('刷新会放弃尚未保存的表单修改，继续吗？')) return; try { await refresh(); message('状态已刷新。'); } catch (e) { message(e.message, true); } });
$('logout').addEventListener('click', async () => { try { await api('logout', {}); csrf = ''; signedIn(false); message('已退出登录。'); } catch (e) { message(e.message, true); } });
$('password-form').addEventListener('submit', e => { e.preventDefault(); busy(e.currentTarget, async () => { if ($('new-password').value !== $('confirm-password').value) throw Error('两次输入的新密码不一致'); await api('password', { old_password: $('old-password').value, new_password: $('new-password').value }); $('password-form').reset(); await refresh(); message('管理密码已更新，其他后台会话已退出。'); }); });
refresh().catch(e => { signedIn(false); if (e.message !== '请先登录管理后台') message(e.message, true); });
