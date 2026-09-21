/* 拾音 · 前端交互
   与本地 Python 服务同源通信，所有写操作携带 X-CSRF-Token。 */
'use strict';

const state = {
  csrf: '', demo: false, user: null, settings: null, qualities: [], apiAvailable: true,
  playlists: [], filter: 'all', query: '',
  current: null, selected: new Set(), importMode: 'playlist',
  queue: null, queueFilter: 'all', queueQuery: '',
  queueTimer: null, qrTimer: null, smsTimer: null,
  dirty: false, polling: false, openingId: null,
};

const STATUS = {
  queued: '等待中', resolving: '解析地址', downloading: '下载中', tagging: '写入标签',
  completed: '已完成', skipped: '已存在', failed: '失败', paused: '已暂停', cancelled: '已取消',
};
const FINISHED = new Set(['completed', 'skipped', 'cancelled']);

/* ---------- 基础工具 ---------- */
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
const NS = 'http://www.w3.org/2000/svg';
function icon(name) {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('class', 'icon');
  const use = document.createElementNS(NS, 'use');
  use.setAttribute('href', '#i-' + name);
  svg.appendChild(use);
  return svg;
}
function setBusy(button, busy, label) {
  if (!button) return;
  if (busy) {
    button.dataset.label = button.dataset.label || '';
    button.disabled = true;
    button.classList.add('is-loading');
    if (!button.querySelector('.spinner')) button.prepend(el('span', 'spinner'));
  } else {
    button.disabled = false;
    button.classList.remove('is-loading');
    const spinner = button.querySelector('.spinner');
    if (spinner) spinner.remove();
  }
  if (label !== undefined && label !== null) {
    const span = button.querySelector('span:not(.spinner)');
    if (span) span.textContent = label;
    else {
      const textNode = Array.from(button.childNodes).find((n) => n.nodeType === 3 && n.textContent.trim());
      if (textNode) textNode.textContent = label;
    }
  }
}
function show(node, visible) { if (node) node.hidden = !visible; }
function text(node, value) { if (node) node.textContent = value === undefined || value === null ? '' : String(value); }

function toast(message, type, action) {
  const stack = $('#toasts');
  if (!stack) return;
  const box = el('div', 'toast' + (type === 'error' ? ' toast-error' : ''));
  box.setAttribute('role', type === 'error' ? 'alert' : 'status');
  box.appendChild(icon(type === 'error' ? 'info' : 'check'));
  box.appendChild(el('span', null, message));
  let timer = null;
  const dismiss = () => { clearTimeout(timer); box.remove(); };
  if (action) {
    // 带操作的提示要留够时间给用户点（默认 5 秒太短）
    const button = el('button', 'text-button toast-action', action.label);
    button.type = 'button';
    button.addEventListener('click', () => { dismiss(); action.onClick(); });
    box.appendChild(button);
  }
  const close = el('button', 'icon-button');
  close.setAttribute('aria-label', '关闭提示');
  close.appendChild(icon('close'));
  close.addEventListener('click', dismiss);
  box.appendChild(close);
  stack.appendChild(box);
  timer = setTimeout(dismiss, action ? 15000 : (type === 'error' ? 9000 : 5000));
}

async function api(path, options) {
  const opts = options || {};
  const send = async (retry) => {
    const init = { method: opts.method || 'GET', headers: {}, credentials: 'same-origin' };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    if (init.method !== 'GET') init.headers['X-CSRF-Token'] = state.csrf;
    let response;
    try {
      response = await fetch('/api' + path, init);
    } catch (error) {
      throw new Error('无法连接本地服务，请确认服务仍在运行。');
    }
    // 服务重启会换掉令牌，此时自动取一次新令牌再重试，避免页面卡在 403
    if (response.status === 403 && retry) {
      await refreshCsrf();
      return send(false);
    }
    let data = null;
    try { data = await response.json(); } catch (error) { data = null; }
    if (!response.ok) {
      const message = data && data.error ? data.error : '请求未完成（' + response.status + '）';
      const failure = new Error(message);
      failure.status = response.status;
      throw failure;
    }
    return data || {};
  };
  return send(true);
}

async function refreshCsrf() {
  try {
    const response = await fetch('/api/bootstrap', { credentials: 'same-origin' });
    if (response.ok) {
      const data = await response.json();
      if (data && data.csrf) state.csrf = data.csrf;
    }
  } catch (error) { /* 仍失败则按原错误上报 */ }
}

function errorMessage(error) {
  return error && error.message ? error.message : '操作未完成，请重试。';
}
function setInline(nodeId, message) {
  const node = document.getElementById(nodeId);
  if (!node) return;
  if (message) { text(node, message); show(node, true); } else { show(node, false); }
}

function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes < 1024) return bytes + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = bytes / 1024, index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return (size >= 10 ? Math.round(size) : size.toFixed(1)) + ' ' + units[index];
}
function formatSpeed(value) { return formatBytes(value) + '/s'; }
function formatDuration(ms) {
  const total = Math.round((Number(ms) || 0) / 1000);
  if (!total) return '--:--';
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return String(minutes).padStart(2, '0') + ':' + String(seconds).padStart(2, '0');
}
function qualityLabel(value) {
  const match = state.qualities.find((item) => item.value === value);
  if (match) return match.label;
  const fallback = { standard: '标准音质', higher: '较高音质', exhigh: '极高音质', lossless: '无损音质', hires: 'Hi-Res', jyeffect: '高清臻音', sky: '沉浸环绕', dolby: '杜比全景声', jymaster: '超清母带' };
  return fallback[value] || value || '未知音质';
}
function parsePlaylistId(value) {
  const raw = String(value || '').trim();
  if (/^\d+$/.test(raw)) return Number(raw);
  const match = raw.match(/(\d{4,})/);
  return match ? Number(match[1]) : null;
}

/* 单曲链接形如 .../song?id=123、.../#/song?id=123，也允许直接填 ID */
function parseSongId(value) {
  const raw = String(value || '').trim();
  if (/^\d+$/.test(raw)) return Number(raw);
  const byId = raw.match(/[?&#/]id=(\d{4,})/) || raw.match(/\/song\/(\d{4,})/);
  if (byId) return Number(byId[1]);
  const match = raw.match(/(\d{4,})/);
  return match ? Number(match[1]) : null;
}

/* ---------- 视图切换 ---------- */
function switchView(name) {
  $$('.nav-item').forEach((button) => {
    const active = button.dataset.view === name;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
  [['library', '我的歌单'], ['queue', '下载队列'], ['settings', '下载设置']].forEach(([key, label]) => {
    const section = document.getElementById('view-' + key);
    if (!section) return;
    show(section, key === name);
    if (key === name) text($('#page-label'), label);
  });
  if (name === 'queue') { renderQueue(); }
  if (name === 'settings') { loadSettings(); }
}

/* ---------- 账号 ---------- */
function renderUser() {
  const user = state.user;
  text($('#account-name'), user ? user.nickname || ('用户 ' + user.userId) : '登录账号');
  const avatar = $('#account-avatar');
  if (avatar) {
    avatar.textContent = '';
    if (user && user.avatarUrl) {
      const img = el('img');
      img.src = user.avatarUrl;
      img.alt = '';
      img.referrerPolicy = 'no-referrer';
      avatar.appendChild(img);
    } else {
      avatar.appendChild(icon('user'));
    }
  }
  show($('#demo-badge'), state.demo);
  show($('#demo-notice'), state.demo);
  show($('#auth-demo-notice'), state.demo);
  const profileAvatar = $('#profile-avatar');
  if (profileAvatar) {
    profileAvatar.textContent = '';
    if (user && user.avatarUrl) {
      const img = el('img');
      img.src = user.avatarUrl;
      img.alt = '';
      img.referrerPolicy = 'no-referrer';
      profileAvatar.appendChild(img);
    } else {
      profileAvatar.appendChild(icon('user'));
    }
  }
  text($('#profile-name'), user ? user.nickname || '已登录' : '未登录');
  text($('#profile-id'), user ? 'UID ' + user.userId : '登录后可查看个人歌单');
}

function openDialog(id) {
  const dialog = document.getElementById(id);
  if (dialog && typeof dialog.showModal === 'function') dialog.showModal();
}
function closeDialog(id) {
  const dialog = document.getElementById(id);
  if (dialog) dialog.close();
}

async function openAuth() {
  setInline('auth-error', '');
  openDialog('auth-dialog');
  if (state.demo) { text($('#qr-status'), '演示模式：无需真实扫码'); return; }
  startQr();
}

async function startQr() {
  stopQr();
  const frame = $('#qr-frame');
  if (frame) { frame.textContent = ''; frame.appendChild(icon('qr')); }
  text($('#qr-status'), '正在生成登录二维码…');
  show($('#qr-open-link'), false);
  try {
    const data = await api('/auth/qr', { method: 'POST', body: {} });
    if (frame) { frame.textContent = ''; const img = el('img'); img.src = data.image; img.alt = '登录二维码'; frame.appendChild(img); }
    // 只接受 http(s) 链接，避免把接口返回的伪协议直接塞进 href
    if (data.url && /^https?:\/\//i.test(data.url)) {
      const link = $('#qr-open-link');
      if (link) { link.href = data.url; show(link, true); }
    }
    text($('#qr-status'), '使用网易云音乐 App 扫码');
    state.qrKey = data.key;
    state.qrTimer = setInterval(checkQr, 2000);
  } catch (error) {
    text($('#qr-status'), errorMessage(error));
  }
}
function stopQr() { if (state.qrTimer) { clearInterval(state.qrTimer); state.qrTimer = null; } }

async function checkQr() {
  if (!state.qrKey) return;
  try {
    const data = await api('/auth/qr/check', { method: 'POST', body: { key: state.qrKey } });
    text($('#qr-status'), data.message || '等待扫码');
    if (data.code === 803 && data.user) {
      stopQr();
      state.user = data.user;
      renderUser();
      closeDialog('auth-dialog');
      toast('登录成功，欢迎回来');
      await loadPlaylists();
    } else if (data.code === 800) {
      stopQr();
    }
  } catch (error) {
    stopQr();
    text($('#qr-status'), errorMessage(error));
  }
}

async function sendSms() {
  const button = $('#send-sms');
  const phone = $('#sms-phone').value.trim();
  const country = $('#sms-country').value.trim() || '86';
  setInline('auth-error', '');
  setBusy(button, true);
  try {
    await api('/auth/sms/send', { method: 'POST', body: { phone: phone, countrycode: country } });
    text($('#sms-status'), '验证码已发送，请查看手机短信');
    let remain = 60;
    if (state.smsTimer) clearInterval(state.smsTimer);
    text(button, '重新发送（' + remain + 's）');
    state.smsTimer = setInterval(() => {
      remain -= 1;
      if (remain <= 0) { clearInterval(state.smsTimer); state.smsTimer = null; text(button, '获取验证码'); button.disabled = false; return; }
      text(button, '重新发送（' + remain + 's）');
    }, 1000);
  } catch (error) {
    setInline('auth-error', errorMessage(error));
  } finally {
    setBusy(button, false);
  }
}

async function smsLogin(event) {
  event.preventDefault();
  const button = $('#sms-login-button');
  setInline('auth-error', '');
  setBusy(button, true);
  try {
    const data = await api('/auth/sms/login', {
      method: 'POST',
      body: { phone: $('#sms-phone').value.trim(), countrycode: $('#sms-country').value.trim() || '86', captcha: $('#sms-captcha').value.trim() },
    });
    state.user = data.user;
    renderUser();
    closeDialog('auth-dialog');
    toast('登录成功，欢迎回来');
    await loadPlaylists();
  } catch (error) {
    setInline('auth-error', errorMessage(error));
  } finally {
    setBusy(button, false);
  }
}

async function loginFromSplayer() {
  // 有意保持安静：读不到就什么都不说，只有成功时才提示
  const button = $('#login-from-splayer');
  if (!state.settings || !state.settings.splayer_login) return;
  setBusy(button, true);
  try {
    const data = await api('/auth/from-splayer', { method: 'POST', body: {} });
    state.user = data.user;
    renderUser();
    closeDialog('auth-dialog');
    toast('已读取 SPlayer 的登录状态', 'ok', data.can_undo ? {
      label: '撤回',
      onClick: undoImport,
    } : null);
    await loadPlaylists();
  } catch (error) {
    /* 失败不提示：这不是一个需要用户处理的操作 */
  } finally {
    setBusy(button, false);
  }
}

async function undoImport() {
  try {
    const data = await api('/auth/from-splayer/undo', { method: 'POST', body: {} });
    state.user = data.user || null;
    renderUser();
    state.playlists = [];
    state.current = null;
    renderPlaylists();
    show($('#playlist-detail'), false);
    show($('#library-overview'), true);
    toast(data.user ? '已撤回，恢复为 ' + (data.user.nickname || '上一个账号') : '已撤回，恢复到之前的未登录状态');
    if (state.user) await loadPlaylists();
  } catch (error) {
    toast(errorMessage(error), 'error');
  }
}

/** 设置里关掉「从 SPlayer 读取登录状态」时，把入口一并隐藏。 */
function syncSplayerEntry() {
  const button = $('#login-from-splayer');
  if (button) button.hidden = !(state.settings && state.settings.splayer_login);
}

  event.preventDefault();
async function cookieLogin(event) {
  const button = $('#cookie-login-button');
  const input = $('#cookie-input');
  setInline('auth-error', '');
  setBusy(button, true);
  try {
    const data = await api('/auth/cookie', { method: 'POST', body: { cookie: input.value } });
    input.value = '';
    state.user = data.user;
    renderUser();
    closeDialog('auth-dialog');
    toast('Cookie 登录成功');
    await loadPlaylists();
  } catch (error) {
    setInline('auth-error', errorMessage(error));
  } finally {
    setBusy(button, false);
  }
}

async function verifySession() {
  const button = $('#verify-session');
  setInline('account-error', '');
  setBusy(button, true);
  try {
    const data = await api('/session');
    state.user = data.user;
    renderUser();
    toast(data.user ? '登录状态有效：' + (data.user.nickname || data.user.userId) : '登录已失效，请重新登录');
    if (!data.user) { closeDialog('account-dialog'); await loadPlaylists(); }
  } catch (error) {
    setInline('account-error', errorMessage(error));
  } finally {
    setBusy(button, false);
  }
}

async function logout() {
  const confirmed = await confirmAction('退出登录', '退出后将无法查看个人歌单，已下载的文件不受影响。确认退出当前账号？', '退出登录');
  if (!confirmed) return;
  try {
    await api('/auth/logout', { method: 'POST', body: {} });
    state.user = null;
    state.playlists = [];
    renderUser();
    closeDialog('account-dialog');
    renderPlaylists();
    toast('已退出登录');
  } catch (error) {
    setInline('account-error', errorMessage(error));
  }
}

/* 确认对话框 */
let confirmResolver = null;
function confirmAction(title, description, confirmLabel) {
  text($('#confirm-title'), title);
  text($('#confirm-description'), description);
  const yes = $('#confirm-yes');
  text(yes, confirmLabel || '确认');
  const dialog = $('#confirm-dialog');
  openDialog('confirm-dialog');
  return new Promise((resolve) => { confirmResolver = resolve; });
}
function resolveConfirm(value) {
  if (confirmResolver) { confirmResolver(value); confirmResolver = null; }
  closeDialog('confirm-dialog');
}

/* ---------- 歌单 ---------- */
function renderPlaylists() {
  const grid = $('#playlist-grid');
  if (!grid) return;
  grid.textContent = '';
  grid.setAttribute('aria-busy', 'false');

  const term = state.query.trim().toLowerCase();
  const list = state.playlists.filter((item) => {
    if (state.filter === 'owned' && !item.owned) return false;
    if (state.filter === 'saved' && item.owned) return false;
    if (term && !String(item.name || '').toLowerCase().includes(term)) return false;
    return true;
  });

  text($('#playlist-count'), state.playlists.length);
  show($('#library-empty'), state.playlists.length === 0);
  if (state.playlists.length === 0) {
    const emptyTitle = $('#library-empty-title');
    const emptyText = $('#library-empty-text');
    const loginButton = $('#library-login');
    if (state.user) {
      text(emptyTitle, '这个账号还没有歌单');
      text(emptyText, '在网易云音乐里创建或收藏歌单后，点击“刷新歌单”即可看到；也可以在上方导入公开歌单。');
      show(loginButton, false);
    } else {
      text(emptyTitle, '让你的音乐收藏在这里相遇');
      text(emptyText, '登录网易云账号，获取你的歌单；也可以在上方导入公开歌单。');
      show(loginButton, true);
    }
    text($('#library-subtitle'), '登录后可获取你创建、收藏和喜欢的歌单。');
    return;
  }
  text($('#library-subtitle'), '每一份歌单，都有自己的故事。');

  if (!list.length) {
    const box = el('div', 'small-empty', '没有匹配的歌单，试试其他关键词。');
    box.style.gridColumn = '1 / -1';
    grid.appendChild(box);
    return;
  }

  list.forEach((item) => {
    const card = el('button', 'playlist-card');
    card.type = 'button';
    const cover = el('span', 'playlist-cover');
    if (item.coverImgUrl) {
      const img = el('img');
      img.src = item.coverImgUrl + (item.coverImgUrl.includes('?') ? '&' : '?') + 'param=400y400';
      img.alt = '';
      img.loading = 'lazy';
      img.referrerPolicy = 'no-referrer';
      img.addEventListener('error', () => { img.remove(); const fb = el('span', 'cover-fallback'); fb.appendChild(icon('note')); cover.prepend(fb); });
      cover.appendChild(img);
    } else {
      const fb = el('span', 'cover-fallback');
      fb.appendChild(icon('note'));
      cover.appendChild(fb);
    }
    const count = el('span', 'cover-count');
    count.appendChild(icon('note'));
    count.appendChild(el('span', null, (item.trackCount || 0) + ' 首'));
    cover.appendChild(count);
    const open = el('span', 'cover-open');
    open.appendChild(icon('arrow'));
    cover.appendChild(open);
    card.appendChild(cover);
    card.appendChild(el('span', 'playlist-card-name', item.name || '未命名歌单'));
    const creator = item.creator && item.creator.nickname ? item.creator.nickname : '未知创建者';
    card.appendChild(el('span', 'playlist-card-meta', creator + (item.owned ? ' · 我创建的' : ' · 我收藏的')));
    card.addEventListener('click', () => openPlaylist(item.id));
    grid.appendChild(card);
  });
}

async function loadPlaylists(silent) {
  if (!state.user) { state.playlists = []; renderPlaylists(); return; }
  const button = $('#refresh-playlists');
  setInline('library-error', '');
  if (!silent) setBusy(button, true);
  try {
    const data = await api('/playlists');
    state.playlists = data.playlists || [];
    renderPlaylists();
  } catch (error) {
    setInline('library-error', errorMessage(error));
    state.playlists = [];
    renderPlaylists();
    if (error.status === 400) { state.user = null; renderUser(); }
  } finally {
    if (!silent) setBusy(button, false);
  }
}

async function openPlaylist(id) {
  state.openingId = id;
  show($('#library-overview'), false);
  show($('#playlist-detail'), true);
  setInline('download-error', '');
  show($('#playlist-warnings'), false);
  text($('#detail-title'), '正在读取歌单…');
  text($('#detail-description'), '');
  text($('#detail-count'), '');
  const cover = $('#detail-cover');
  if (cover) { cover.textContent = ''; cover.appendChild(icon('note')); }
  const rows = $('#song-rows');
  if (rows) rows.textContent = '';
  state.current = null;
  state.selected = new Set();
  text($('#selection-count'), '已选 0 首');
  const downloadAll = $('#download-playlist');
  if (downloadAll) downloadAll.disabled = true;
  const downloadSelected = $('#download-selected');
  if (downloadSelected) downloadSelected.disabled = true;

  try {
    const data = await api('/playlists/' + id);
    state.current = data;
    const playlist = data.playlist || {};
    text($('#detail-title'), playlist.name || '歌单');
    text($('#detail-description'), playlist.description || '');
    text($('#detail-count'), (data.songs || []).length + ' 首歌曲');
    if (cover) {
      cover.textContent = '';
      if (playlist.coverImgUrl) {
        const img = el('img');
        img.src = playlist.coverImgUrl + (playlist.coverImgUrl.includes('?') ? '&' : '?') + 'param=400y400';
        img.alt = '';
        img.referrerPolicy = 'no-referrer';
        cover.appendChild(img);
      } else {
        cover.appendChild(icon('note'));
      }
    }
    const warnings = data.warnings || [];
    if (warnings.length) {
      const box = $('#playlist-warnings');
      if (box) { box.textContent = ''; box.appendChild(icon('info')); box.appendChild(el('span', null, warnings.join('\n'))); show(box, true); }
    }
    if (downloadAll) downloadAll.disabled = !(data.songs || []).length;
    renderSongs();
  } catch (error) {
    text($('#detail-title'), '无法读取歌单');
    setInline('download-error', errorMessage(error));
  }
}

function filteredSongs() {
  const term = $('#song-search') ? $('#song-search').value.trim().toLowerCase() : '';
  const songs = state.current ? state.current.songs || [] : [];
  if (!term) return songs;
  return songs.filter((song) => (song.name + ' ' + song.artists + ' ' + song.album).toLowerCase().includes(term));
}

function renderSongs() {
  const rows = $('#song-rows');
  if (!rows || !state.current) return;
  rows.textContent = '';
  const songs = filteredSongs();
  text($('#song-filter-count'), '共 ' + songs.length + ' 首');
  show($('#songs-empty'), songs.length === 0);

  songs.forEach((song, index) => {
    const tr = el('tr');
    if (state.selected.has(song.id)) tr.classList.add('selected');

    const selectCell = el('td', 'song-select-col');
    const checkbox = el('input');
    checkbox.type = 'checkbox';
    checkbox.checked = state.selected.has(song.id);
    checkbox.setAttribute('aria-label', '选择 ' + song.name);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) state.selected.add(song.id); else state.selected.delete(song.id);
      tr.classList.toggle('selected', checkbox.checked);
      updateSelection();
    });
    selectCell.appendChild(checkbox);
    tr.appendChild(selectCell);

    tr.appendChild(el('td', 'song-index', String(index + 1)));

    const nameCell = el('td');
    const wrap = el('div', 'song-name-cell');
    const art = el('div', 'song-art');
    if (song.cover) {
      const img = el('img');
      img.src = song.cover + (song.cover.includes('?') ? '&' : '?') + 'param=90y90';
      img.alt = '';
      img.loading = 'lazy';
      img.referrerPolicy = 'no-referrer';
      img.addEventListener('error', () => { img.remove(); art.appendChild(icon('note')); });
      art.appendChild(img);
    } else {
      art.appendChild(icon('note'));
    }
    wrap.appendChild(art);
    const textBox = el('div', 'song-text');
    textBox.appendChild(el('strong', null, song.name));
    textBox.appendChild(el('span', null, song.artists));
    wrap.appendChild(textBox);
    nameCell.appendChild(wrap);
    tr.appendChild(nameCell);

    tr.appendChild(el('td', 'song-album', song.album));
    tr.appendChild(el('td', 'song-duration', formatDuration(song.duration)));
    rows.appendChild(tr);
  });
  updateSelection();
}

function updateSelection() {
  text($('#selection-count'), '已选 ' + state.selected.size + ' 首');
  const button = $('#download-selected');
  if (button) button.disabled = state.selected.size === 0;
  const all = $('#select-all-songs');
  const songs = filteredSongs();
  if (all) {
    const chosen = songs.filter((song) => state.selected.has(song.id)).length;
    all.checked = songs.length > 0 && chosen === songs.length;
    all.indeterminate = chosen > 0 && chosen < songs.length;
  }
}

async function downloadSingleSong(songId) {
  const button = $('#open-playlist-button');
  setInline('share-error', '');
  setBusy(button, true);
  try {
    const data = await api('/downloads', { method: 'POST', body: { song_ids: [songId] } });
    toast('已加入 ' + data.added + ' 首歌曲' + (data.existing ? '，已在队列或已下载' : '')
      + (data.missing ? '，' + data.missing + ' 首无法获取' : ''));
    switchView('queue');
    await pollQueue(true);
  } catch (error) {
    setInline('share-error', errorMessage(error));
  } finally {
    setBusy(button, false);
  }
}

async function enqueue(songIds) {
  if (!state.current) return;
  const body = { playlist_id: state.current.playlist.id };
  if (songIds) body.song_ids = songIds;
  const button = songIds ? $('#download-selected') : $('#download-playlist');
  setInline('download-error', '');
  setBusy(button, true);
  try {
    const data = await api('/downloads', { method: 'POST', body: body });
    toast('已加入 ' + data.added + ' 首歌曲' + (data.existing ? '，' + data.existing + ' 首已在队列或已下载' : ''));
    switchView('queue');
    await pollQueue(true);
  } catch (error) {
    setInline('download-error', errorMessage(error));
  } finally {
    setBusy(button, false);
  }
}

/* ---------- 下载队列 ---------- */
function setNavCount(summary) {
  // 徽章表示“还有多少任务没结束”，下载中也算，否则下载时显示 0 会让人以为没事可做
  const pending = (summary.queued || 0) + (summary.active || 0);
  const badge = $('#nav-queue-count');
  if (!badge) return;
  badge.textContent = String(pending);
  badge.hidden = pending === 0;
}

function renderQueue() {
  const snapshot = state.queue;
  if (!snapshot) return;
  const summary = snapshot.summary || {};
  setNavCount(summary);
  text($('#stat-total'), summary.total || 0);
  text($('#stat-active'), summary.active || 0);
  text($('#stat-completed'), (summary.completed || 0) + (summary.skipped || 0));
  text($('#stat-failed'), summary.failed || 0);

  const jobs = snapshot.jobs || [];
  const speed = jobs.reduce((total, job) => total + (['downloading', 'tagging', 'resolving'].includes(job.status) ? Number(job.speed) || 0 : 0), 0);
  text($('#queue-speed'), formatSpeed(speed));

  const total = jobs.length;
  const finished = (summary.completed || 0) + (summary.skipped || 0) + (summary.failed || 0) + (summary.cancelled || 0);
  const percent = total ? Math.round(jobs.reduce((sum, job) => sum + (FINISHED.has(job.status) ? 100 : Number(job.progress) || 0), 0) / total) : 0;
  text($('#queue-percent'), String(percent));
  const fill = $('#overall-progress-fill');
  if (fill) fill.style.width = percent + '%';
  const track = $('#overall-progress');
  if (track) track.setAttribute('aria-valuenow', String(percent));
  text($('#queue-completion-text'), finished + ' / ' + total + ' 已完成');
  text($('#queue-updated'), '更新于 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false }));

  const badge = $('#queue-state');
  if (badge) {
    badge.className = 'badge';
    let label = '队列就绪';
    if (snapshot.paused && (summary.queued || 0) > 0) { label = '已暂停'; badge.classList.add('badge-amber'); }
    else if ((summary.active || 0) > 0) { label = '下载中'; badge.classList.add('badge-green'); }
    else if ((summary.failed || 0) > 0) { label = '有失败任务'; badge.classList.add('badge-red'); }
    else if (total > 0 && finished === total) { label = '全部完成'; badge.classList.add('badge-green'); }
    text(badge, label);
  }
  const summaryText = $('#queue-summary-text');
  if (summaryText) {
    if (!total) text(summaryText, '添加歌曲后，将自动开始下载。');
    else if (snapshot.paused) text(summaryText, '队列已暂停，恢复后会继续等待中的任务。');
    else if ((summary.active || 0) > 0) text(summaryText, summary.active + ' 首正在下载，' + (summary.queued || 0) + ' 首等待中。');
    else if ((summary.failed || 0) > 0) text(summaryText, '有 ' + summary.failed + ' 首下载失败，可重试。');
    else text(summaryText, '全部任务已处理完成。');
  }

  show($('#queue-paused-notice'), !!snapshot.paused && (summary.queued || 0) > 0);

  const toggle = $('#toggle-queue');
  if (toggle) {
    toggle.disabled = total === 0;
    const paused = !!snapshot.paused;
    const label = toggle.querySelector('span');
    if (label) label.textContent = paused ? '恢复队列' : '暂停队列';
    const use = toggle.querySelector('use');
    if (use) use.setAttribute('href', paused ? '#i-play' : '#i-pause');
  }
  const retry = $('#retry-failed');
  if (retry) retry.disabled = !(summary.failed || 0);
  const clear = $('#clear-finished');
  if (clear) clear.disabled = finished === 0;
  const cancelAll = $('#cancel-all');
  if (cancelAll) cancelAll.disabled = total === 0;

  renderJobs(jobs);
}

function matchesFilter(job, paused) {
  switch (state.queueFilter) {
    case 'active': return ['resolving', 'downloading', 'tagging'].includes(job.status);
    case 'queued': return job.status === 'queued' && !paused;
    case 'completed': return ['completed', 'skipped'].includes(job.status);
    case 'failed': return job.status === 'failed';
    case 'paused': return job.status === 'queued' && paused;
    case 'other': return ['cancelled'].includes(job.status);
    default: return true;
  }
}

function renderJobs(jobs) {
  const list = $('#job-list');
  if (!list) return;
  list.textContent = '';
  list.setAttribute('aria-busy', 'false');
  const term = state.queueQuery.trim().toLowerCase();
  const paused = state.queue && state.queue.paused;
  const visible = jobs.filter((job) => {
    if (!matchesFilter(job, paused)) return false;
    if (term && !(job.name + ' ' + job.artists + ' ' + job.album + ' ' + job.playlist).toLowerCase().includes(term)) return false;
    return true;
  });

  show($('#queue-empty'), jobs.length === 0);
  if (jobs.length === 0) {
    if (state.queueFilter === 'all' && !term) {
      text($('#queue-empty-title'), '还没有下载任务');
      text($('#queue-empty-text'), '去歌单里选几首喜欢的歌，让音乐开始抵达。');
    } else {
      text($('#queue-empty-title'), '没有符合条件的任务');
      text($('#queue-empty-text'), '试试切换状态筛选或清空搜索关键词。');
    }
    return;
  }

  visible.forEach((job) => {
    const card = el('article', 'job-card');
    const iconBox = el('div', 'job-icon');
    iconBox.appendChild(icon(job.status === 'failed' ? 'info' : job.status === 'completed' || job.status === 'skipped' ? 'check' : job.status === 'cancelled' ? 'close' : 'download'));
    card.appendChild(iconBox);

    const main = el('div', 'job-main');
    const titleLine = el('div', 'job-title-line');
    titleLine.appendChild(el('h3', null, job.name));
    const stateBadge = el('span', 'badge' + (job.status === 'failed' ? ' badge-red' : job.status === 'completed' || job.status === 'skipped' ? ' badge-green' : job.status === 'cancelled' ? '' : ' badge-amber'), STATUS[job.status] || job.status);
    titleLine.appendChild(stateBadge);
    main.appendChild(titleLine);
    main.appendChild(el('div', 'job-meta', (job.artists || '未知歌手') + ' · ' + (job.album || '未知专辑') + ' · 来自「' + (job.playlist || '歌单') + '」'));

    const track = el('div', 'progress-track job-progress');
    track.setAttribute('role', 'progressbar');
    track.setAttribute('aria-valuemin', '0');
    track.setAttribute('aria-valuemax', '100');
    const scaled = FINISHED.has(job.status) ? 100 : Math.round(Number(job.progress) || 0);
    track.setAttribute('aria-valuenow', String(scaled));
    const bar = el('span');
    bar.style.width = scaled + '%';
    track.appendChild(bar);
    main.appendChild(track);

    const bottom = el('div', 'job-bottom');
    const left = el('span', null, (job.status === 'downloading' || job.status === 'tagging') && job.total
      ? formatBytes(job.downloaded) + ' / ' + formatBytes(job.total) + ' · ' + formatSpeed(job.speed)
      : '音质 ' + qualityLabel(job.quality) + (job.actual_quality && job.actual_quality !== job.quality ? ' · 实际 ' + job.actual_quality : ''));
    bottom.appendChild(left);
    if (job.attempt > 1) bottom.appendChild(el('span', null, '第 ' + job.attempt + ' 次尝试'));
    main.appendChild(bottom);

    if (job.error) main.appendChild(el('div', 'job-error', job.error));
    (job.warnings || []).forEach((warning) => main.appendChild(el('div', 'job-warning', warning)));
    if (job.path && (job.status === 'completed' || job.status === 'skipped')) {
      const details = el('details', 'job-details');
      details.appendChild(el('summary', null, '保存位置'));
      details.appendChild(el('p', null, job.path));
      main.appendChild(details);
    }
    card.appendChild(main);

    const actions = el('div', 'job-actions');
    if (job.status === 'failed' || job.status === 'cancelled') {
      const retry = el('button', 'button button-secondary');
      retry.type = 'button';
      retry.appendChild(icon('refresh'));
      retry.appendChild(el('span', null, '重试'));
      retry.addEventListener('click', () => jobAction(job.id, 'retry', retry));
      actions.appendChild(retry);
    }
    if (['queued', 'resolving', 'downloading', 'tagging'].includes(job.status)) {
      const cancel = el('button', 'button button-secondary');
      cancel.type = 'button';
      cancel.appendChild(icon('close'));
      cancel.appendChild(el('span', null, '取消'));
      cancel.addEventListener('click', () => jobAction(job.id, 'cancel', cancel));
      actions.appendChild(cancel);
    }
    card.appendChild(actions);
    list.appendChild(card);
  });
}

async function jobAction(id, action, button) {
  setInline('queue-error', '');
  setBusy(button, true);
  try {
    await api('/downloads/' + id + '/action', { method: 'POST', body: { action: action } });
    await pollQueue(true);
  } catch (error) {
    setInline('queue-error', errorMessage(error));
  } finally {
    setBusy(button, false);
  }
}

async function queueAction(action, button, label) {
  if (action === 'cancel_all') {
    const confirmed = await confirmAction('取消全部任务', '正在下载的文件会被中断，等待中的任务不再开始。已下载完成的文件不受影响。确认取消全部任务？', '取消全部');
    if (!confirmed) return;
  }
  if (action === 'clear_finished') {
    const confirmed = await confirmAction('清理已结束任务', '将从队列中移除已完成、已存在、已取消和失败的任务记录，不会删除任何已下载的文件。确认继续？', '清理记录');
    if (!confirmed) return;
  }
  setInline('queue-error', '');
  setBusy(button, true);
  try {
    await api('/downloads/action', { method: 'POST', body: { action: action } });
    await pollQueue(true);
    if (label) toast(label);
  } catch (error) {
    setInline('queue-error', errorMessage(error));
  } finally {
    setBusy(button, false);
    // 重新按最新队列状态决定按钮可用性，避免重试后按钮仍可点击
    if (state.queue && !$('#view-queue').hidden) renderQueue();
  }
}

async function pollQueue(force) {
  if (state.polling && !force) return;
  state.polling = true;
  try {
    const data = await api('/downloads');
    state.queue = data;
    state.pollFailures = 0;
    setInline('queue-error', '');   // 恢复后清掉断线期间留下的旧错误
    if (!$('#view-queue').hidden) renderQueue();
    else setNavCount(data.summary || {});
  } catch (error) {
    state.pollFailures = (state.pollFailures || 0) + 1;
    if (!force) setInline('queue-error', errorMessage(error));
    // 连续失败说明本地服务已断开，别让界面继续显示重启前的旧进度
    if (state.pollFailures >= 3 && state.queue) {
      state.queue = null;
      setNavCount({});
      if (!$('#view-queue').hidden) {
        text($('#queue-state'), '已断开');
        text($('#queue-summary-text'), '本地服务已断开，恢复后会自动重新同步。');
        text($('#queue-updated'), '数据已过期');
      }
    }
  } finally {
    state.polling = false;
  }
}

function scheduleQueuePolling() {
  const tick = async () => {
    await pollQueue(false);
    const summary = (state.queue && state.queue.summary) || {};
    const busy = (summary.active || 0) + (summary.queued || 0) > 0;
    state.queueTimer = setTimeout(tick, busy ? 1200 : 4000);
  };
  tick();
}

/* ---------- 设置 ---------- */
function fillSettingsForm(settings) {
  const form = $('#settings-form');
  if (!form || !settings) return;
  const set = (id, value) => { const node = document.getElementById(id); if (!node) return; if (node.type === 'checkbox') node.checked = !!value; else node.value = value === undefined || value === null ? '' : value; };
  set('setting-download-dir', settings.download_dir);
  set('setting-quality', settings.quality);
  set('setting-playlist-folder', settings.playlist_folder);
  set('setting-workers', settings.workers);
  set('setting-retries', settings.retries);
  set('setting-save-lrc', settings.save_lrc);
  set('setting-embed-lyrics', settings.embed_lyrics);
  set('setting-translation', settings.translation);
  set('setting-romanization', settings.romanization);
  set('setting-cover', settings.cover);
  set('setting-api-base', settings.api_base);
  set('setting-playback-fallback', settings.playback_fallback);
  set('setting-splayer-login', settings.splayer_login);
  syncSettingEchoes(settings.workers, settings.quality);
  state.settings = settings;
  syncSplayerEntry();
  state.dirty = false;
  show($('#settings-dirty'), false);
  text($('#settings-save-status'), '已读取当前设置');
}

function syncSettingEchoes(workers, quality) {
  $$('[data-workers]').forEach((node) => text(node, workers));
  text($('#workers-output'), workers + ' 线程');
  text($('#hero-quality'), qualityLabel(quality));
}

async function loadSettings() {
  try {
    const settings = await api('/settings');
    state.settings = settings;
    fillSettingsForm(settings);
    const fieldset = $('#settings-fields');
    if (fieldset) fieldset.disabled = false;
    const save = $('#save-settings');
    if (save) save.disabled = true;
    const reload = $('#reload-settings');
    if (reload) reload.disabled = false;
  } catch (error) {
    text($('#settings-save-status'), errorMessage(error));
  }
}

function collectSettings() {
  const read = (id) => document.getElementById(id);
  return {
    download_dir: read('setting-download-dir').value.trim(),
    quality: read('setting-quality').value,
    playlist_folder: read('setting-playlist-folder').checked,
    workers: Number(read('setting-workers').value),
    retries: Number(read('setting-retries').value),
    save_lrc: read('setting-save-lrc').checked,
    embed_lyrics: read('setting-embed-lyrics').checked,
    translation: read('setting-translation').checked,
    romanization: read('setting-romanization').checked,
    cover: read('setting-cover').checked,
    api_base: read('setting-api-base').value.trim(),
    playback_fallback: read('setting-playback-fallback').checked,
    splayer_login: read('setting-splayer-login').checked,
  };
}

async function saveSettings(event) {
  event.preventDefault();
  const button = $('#save-settings');
  setInline('settings-error', '');
  setInline('folder-error', '');
  setBusy(button, true);
  try {
    const settings = await api('/settings', { method: 'PATCH', body: collectSettings() });
    state.settings = settings;
    fillSettingsForm(settings);
    text($('#settings-save-status'), '设置已保存 · ' + new Date().toLocaleTimeString('zh-CN', { hour12: false }));
    toast('设置已保存');
    checkApi();
  } catch (error) {
    if (/目录/.test(errorMessage(error))) setInline('folder-error', errorMessage(error));
    else setInline('settings-error', errorMessage(error));
  } finally {
    setBusy(button, false);
  }
}

async function pickFolder() {
  const button = $('#pick-folder');
  setInline('folder-error', '');
  setBusy(button, true);
  try {
    const data = await api('/folder/pick', { method: 'POST', body: {} });
    if (data.cancelled || !data.path) return;
    const input = $('#setting-download-dir');
    if (input) { input.value = data.path; markDirty(); }
  } catch (error) {
    setInline('folder-error', errorMessage(error));
  } finally {
    setBusy(button, false);
  }
}
function markDirty() {
  state.dirty = true;
  show($('#settings-dirty'), true);
  const save = $('#save-settings');
  if (save) save.disabled = false;
  text($('#settings-save-status'), '有未保存的更改');
}

/* ---------- 服务状态 ---------- */
async function checkApi() {
  const badge = $('#api-status');
  if (!badge) return;
  try {
    // Probing the music API directly from the page is cross-origin; ask the
    // local service instead, which reaches api_base server-side.
    const data = await api('/bootstrap');
    const ok = !!data.api_available;
    state.apiAvailable = ok;
    badge.textContent = '';
    badge.appendChild(el('span', 'status-dot' + (ok ? '' : ' offline')));
    badge.appendChild(el('span', null, state.demo ? '演示音频源' : ok ? '音乐接口已连接' : '音乐接口未连接'));
  } catch (error) {
    badge.textContent = '';
    badge.appendChild(el('span', 'status-dot offline'));
    badge.appendChild(el('span', null, '本地服务异常'));
  }
}

/* ---------- 事件绑定 ---------- */
function bind() {
  $$('.nav-item').forEach((button) => button.addEventListener('click', () => switchView(button.dataset.view)));
  $$('[data-view]:not(.nav-item)').forEach((button) => button.addEventListener('click', (event) => {
    event.preventDefault();
    switchView(button.dataset.view);
  }));

  $$('[data-import-mode]').forEach((button) => button.addEventListener('click', () => {
    state.importMode = button.dataset.importMode;
    $$('[data-import-mode]').forEach((other) => {
      const active = other === button;
      other.classList.toggle('active', active);
      other.setAttribute('aria-pressed', String(active));
    });
    const song = state.importMode === 'song';
    text($('#import-label'), song ? '单曲' : '公开歌单');
    text($('#import-action'), song ? '下载单曲' : '解析歌单');
    $('#playlist-link').placeholder = song
      ? '粘贴网易云单曲分享链接，或输入歌曲 ID'
      : '粘贴网易云歌单分享链接，或输入歌单 ID';
    setInline('share-error', '');
  }));

  $('#share-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    setInline('share-error', '');
    const value = $('#playlist-link').value;
    if (state.importMode === 'song') {
      const songId = parseSongId(value);
      if (!songId) { setInline('share-error', '请输入有效的歌曲 ID 或单曲链接'); return; }
      await downloadSingleSong(songId);
      return;
    }
    const id = parsePlaylistId(value);
    if (!id) { setInline('share-error', '请输入有效的歌单 ID 或分享链接'); return; }
    await openPlaylist(id);
  });
  $('#back-to-library').addEventListener('click', () => { show($('#playlist-detail'), false); show($('#library-overview'), true); });

  $('#refresh-playlists').addEventListener('click', async () => {
    // 歌单详情有 10 分钟缓存，“刷新”应同时丢弃它，否则看到的是旧数据
    try { await api('/cache/clear', { method: 'POST', body: {} }); } catch (error) { /* 清缓存失败不影响刷新 */ }
    await loadPlaylists();
  });
  $('#playlist-search').addEventListener('input', (event) => { state.query = event.target.value; renderPlaylists(); });
  $$('[data-library-filter]').forEach((button) => button.addEventListener('click', () => {
    state.filter = button.dataset.libraryFilter;
    $$('[data-library-filter]').forEach((other) => {
      const active = other === button;
      other.classList.toggle('active', active);
      other.setAttribute('aria-pressed', String(active));
    });
    renderPlaylists();
  }));
  $('#library-login').addEventListener('click', openAuth);

  $('#song-search').addEventListener('input', renderSongs);
  $('#select-all-songs').addEventListener('change', (event) => {
    const songs = filteredSongs();
    if (event.target.checked) songs.forEach((song) => state.selected.add(song.id));
    else songs.forEach((song) => state.selected.delete(song.id));
    renderSongs();
  });
  $('#clear-selection').addEventListener('click', () => { state.selected.clear(); renderSongs(); });
  $('#download-selected').addEventListener('click', () => enqueue(Array.from(state.selected)));
  $('#download-playlist').addEventListener('click', () => enqueue(null));

  $('#refresh-queue').addEventListener('click', async (event) => { setBusy(event.currentTarget, true); await pollQueue(true); renderQueue(); setBusy(event.currentTarget, false); });
  $('#export-report').addEventListener('click', () => { window.location.href = '/api/downloads/report'; });
  $('#toggle-queue').addEventListener('click', (event) => {
    const paused = state.queue && state.queue.paused;
    queueAction(paused ? 'resume' : 'pause', event.currentTarget, paused ? '已恢复队列' : '已暂停队列');
  });
  $('#retry-failed').addEventListener('click', (event) => queueAction('retry_failed', event.currentTarget, '已重新排队失败任务'));
  $('#clear-finished').addEventListener('click', (event) => queueAction('clear_finished', event.currentTarget, '已清理结束的任务'));
  $('#cancel-all').addEventListener('click', (event) => queueAction('cancel_all', event.currentTarget, '已取消全部任务'));
  $('#queue-search').addEventListener('input', (event) => { state.queueQuery = event.target.value; renderQueue(); });
  $$('[data-queue-filter]').forEach((button) => button.addEventListener('click', () => {
    state.queueFilter = button.dataset.queueFilter;
    $$('[data-queue-filter]').forEach((other) => {
      const active = other === button;
      other.classList.toggle('active', active);
      other.setAttribute('aria-pressed', String(active));
    });
    renderQueue();
  }));

  $('#settings-form').addEventListener('submit', saveSettings);
  $('#settings-form').addEventListener('input', () => markDirty());
  $('#settings-form').addEventListener('change', () => markDirty());
  $('#setting-workers').addEventListener('input', (event) => {
    $$('[data-workers]').forEach((node) => text(node, event.target.value));
    text($('#workers-output'), event.target.value + ' 线程');
  });
  $('#setting-quality').addEventListener('change', (event) => text($('#hero-quality'), qualityLabel(event.target.value)));
  $('#pick-folder').addEventListener('click', pickFolder);
  $('#reload-settings').addEventListener('click', loadSettings);

  $('#account-button').addEventListener('click', () => { if (state.user) { renderUser(); openDialog('account-dialog'); } else openAuth(); });
  $('#library-login').addEventListener('click', openAuth);
  $('#refresh-qr').addEventListener('click', startQr);
  $('#send-sms').addEventListener('click', sendSms);
  $('#auth-sms').addEventListener('submit', smsLogin);
  $('#auth-cookie').addEventListener('submit', cookieLogin);
  $('#login-from-splayer').addEventListener('click', loginFromSplayer);
  $('#verify-session').addEventListener('click', verifySession);
  $('#logout-button').addEventListener('click', logout);
  $('#reconnect-button').addEventListener('click', async () => { show($('#global-error'), false); await checkApi(); await pollQueue(true); });
  $('#confirm-no').addEventListener('click', () => resolveConfirm(false));
  $('#confirm-yes').addEventListener('click', () => resolveConfirm(true));
  $$('[data-close-dialog]').forEach((button) => button.addEventListener('click', () => {
    closeDialog(button.dataset.closeDialog);
    if (button.dataset.closeDialog === 'auth-dialog') stopQr();
  }));
  $('#auth-dialog').addEventListener('close', stopQr);

  $$('[data-auth-tab]').forEach((tab) => {
    const activate = () => {
      $$('[data-auth-tab]').forEach((other) => {
        const active = other === tab;
        other.setAttribute('aria-selected', String(active));
        other.tabIndex = active ? 0 : -1;
        const panel = document.getElementById('auth-' + other.dataset.authTab);
        if (panel) show(panel, active);
      });
      if (tab.dataset.authTab === 'qr' && !state.demo) startQr(); else stopQr();
    };
    tab.addEventListener('click', activate);
    tab.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
        event.preventDefault();
        const tabs = $$('[data-auth-tab]');
        const index = tabs.indexOf(tab);
        tabs[(index + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length].focus();
      }
    });
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && confirmResolver) resolveConfirm(false);
  });
}

/* ---------- 启动 ---------- */
async function boot() {
  bind();
  try {
    const data = await api('/bootstrap');
    state.csrf = data.csrf;
    state.demo = !!data.demo;
    state.user = data.user || null;
    state.settings = data.settings;
    state.qualities = data.qualities || [];
    state.apiAvailable = !!data.api_available;
    renderUser();
    syncSettingEchoes(state.settings.workers, state.settings.quality);
    syncSplayerEntry();
    show($('#global-error'), !state.apiAvailable);
    if (!state.apiAvailable) text($('#global-error-text'), '未连接到音乐接口。请打开 SPlayer，或在设置中填写独立的 API 服务地址。');
    await checkApi();
    await loadPlaylists(true);
    scheduleQueuePolling();
  } catch (error) {
    text($('#global-error-text'), errorMessage(error));
    show($('#global-error'), true);
  }
}

// 页面可能已从缓存或 BFCache 恢复，此时 DOMContentLoaded 已经错过，必须直接启动
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
