/* unfuckarr web UI — no build step, no dependencies. */

const API = 'api';
let STATUS = null;          // last /api/status payload
let SETTINGS = null;        // last /api/settings payload
let ROUTE = '/';

/* ---------- helpers ---------- */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2).toLowerCase(), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return n;
}

async function api(path, opts = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { /* not JSON */ }
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

function bytes(n) {
  if (!n) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i > 1 ? 1 : 0)} ${u[i]}`;
}

function duration(sec) {
  if (!sec || sec < 0) return '—';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${Math.floor(sec % 60)}s`;
  return `${Math.floor(sec)}s`;
}

function ago(ts) {
  if (!ts) return 'never';
  const d = Date.now() / 1000 - ts;
  if (d < 60) return 'just now';
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}

function when(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : '—';
}

function basename(p) { return (p || '').split('/').pop(); }

/* A whole table row is the click target for opening a file. Rows are not
   focusable by default, so make them reachable from the keyboard too. */
function clickableRow(onOpen, ...cells) {
  return el('tr', {
    class: 'click', tabindex: '0', role: 'button', onclick: onOpen,
    onkeydown: (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(); }
    },
  }, ...cells);
}

function clip(text, n) {
  const s = String(text || '');
  return s.length > n ? `${s.slice(0, n).trimEnd()}…` : s;
}

function toast(msg, kind = '') {
  const t = el('div', { class: `toast ${kind}` }, msg);
  $('#toasts').append(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, 5000);
}

const STATUS_LABEL = {
  ok: 'OK', corrupt: 'Corrupt', incompatible: 'Incompatible',
  hygiene: 'Needs tidying', missing: 'Awaiting replacement',
  unknown: 'Not checked', error: 'Check failed',
};

function statusPill(s) {
  return el('span', { class: `pill pill-${s || 'unknown'}` }, STATUS_LABEL[s] || s || '—');
}

/* ---------- live event stream ---------- */

let es = null;
function connectEvents() {
  if (es) es.close();
  es = new EventSource(`${API}/events`);
  es.onopen = () => $('#conn').classList.add('live');
  es.onerror = () => {
    $('#conn').classList.remove('live');
    // EventSource retries on its own; nothing to do but reflect the state.
  };
  es.onmessage = (e) => {
    let payload;
    try { payload = JSON.parse(e.data); } catch { return; }
    handleEvent(payload.event, payload.data);
  };
}

let refreshTimer = null;
function scheduleRefresh(ms = 800) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refreshStatus, ms);
}

function handleEvent(event, data) {
  switch (event) {
    case 'hello':
    case 'state':
      if (STATUS) { STATUS.state = data; renderLive(); }
      break;
    case 'scan':
      if (STATUS) { STATUS.state.scan = data; renderLive(); }
      // The counts move as a scan progresses; keep them roughly current
      // without a request per file.
      scheduleRefresh(2500);
      break;
    case 'task':
      if (STATUS) {
        if (data.task) STATUS.state.tasks[data.key] = data.task;
        else delete STATUS.state.tasks[data.key];
        renderLive();
      }
      break;
    case 'services':
      if (STATUS) { STATUS.state.services = data; if (ROUTE === '/') render(); }
      break;
    case 'arrival':
      toast(`New file checked: ${basename(data.path)} — ${STATUS_LABEL[data.status] || data.status}`,
        data.status === 'ok' ? 'ok' : 'bad');
      scheduleRefresh();
      break;
    case 'remediated':
      toast(`${data.action}: ${basename(data.path)} — ${data.message || ''}`,
        data.ok ? 'ok' : 'bad');
      scheduleRefresh();
      break;
    case 'job':
      if (ROUTE === '/activity') scheduleRefresh(1500);
      break;
    case 'watchers':
      scheduleRefresh();
      break;
    default:
      break;
  }
}

/* ---------- status / live header ---------- */

async function refreshStatus() {
  try {
    STATUS = await api('/status');
    $('#version').textContent = `v${STATUS.version}`;
    $('#pauseBtn').textContent = STATUS.state.paused ? 'Resume' : 'Pause';
    $('#scanBtn').disabled = STATUS.state.scan.running;
    $('#scanBtn').textContent = STATUS.state.scan.running ? 'Scanning…' : 'Scan now';
    updateBanner();
    if (ROUTE === '/' || ROUTE === '/files') render();
    else renderLive();
  } catch (err) {
    $('#conn').classList.remove('live');
  }
}

function updateBanner() {
  const b = $('#banner');
  const msgs = [];
  const s = STATUS.state;
  if (s.scan.aborted) msgs.push(`Last scan stopped: ${s.scan.aborted}`);
  for (const [name, info] of Object.entries(s.services || {})) {
    if (info.configured && info.ok === false) {
      msgs.push(`${name} is not responding: ${info.error || 'unknown error'}`);
    }
  }
  if (!STATUS.configured) {
    msgs.push('Nothing to scan yet — connect Sonarr or Radarr, or add a library path, in Settings.');
  }
  if (!msgs.length) { b.classList.add('hidden'); return; }
  b.classList.remove('hidden');
  b.classList.toggle('bad', !!s.scan.aborted);
  b.textContent = msgs.join('  •  ');
}

function renderLive() {
  const host = $('#livePanel');
  if (!host || !STATUS) return;
  host.replaceWith(buildLivePanel());
}

function buildLivePanel() {
  const s = STATUS.state;
  const tasks = Object.entries(s.tasks || {});
  const scanning = s.scan.running;

  if (!scanning && !tasks.length) {
    const next = s.next_scan_at
      ? `Next scan ${new Date(s.next_scan_at * 1000).toLocaleString()}`
      : 'Scheduled scans are off';
    return el('div', { class: 'now idle', id: 'livePanel' },
      el('div', { class: 'kind' }, s.paused ? 'Paused' : 'Idle'),
      el('div', { class: 'what' },
        s.last_scan_finished ? `Last scan finished ${ago(s.last_scan_finished)}` : 'No scan yet'),
      el('div', { class: 'detail' }, s.paused ? 'Scheduled scans will not start until you resume.' : next),
      s.watchers?.length
        ? el('div', { class: 'detail' }, `Watching ${s.watchers.length} folder(s): ${s.watchers.join(', ')}`)
        : null,
    );
  }

  const panel = el('div', { class: 'now', id: 'livePanel' });
  if (scanning) {
    const sc = s.scan;
    const frac = sc.total ? sc.checked / sc.total : 0;
    panel.append(
      el('div', { class: 'kind' }, `Scanning — ${sc.trigger}`),
      el('div', { class: 'what' }, sc.current ? basename(sc.current) : 'preparing…'),
      el('div', { class: 'detail' },
        `${sc.checked} of ${sc.total} checked · ${sc.ok} OK · ${sc.failed} with problems · ${sc.actions} action(s) taken`),
      el('div', { class: `bar ${sc.total ? '' : 'indeterminate'}` },
        el('i', { style: `width:${(frac * 100).toFixed(1)}%` })),
    );
  }
  for (const [key, t] of tasks) {
    if (key === 'scan' && scanning) continue;
    panel.append(
      el('div', { class: 'kind', style: 'margin-top:12px' }, t.kind),
      el('div', { class: 'what' }, t.title || basename(t.path)),
      el('div', { class: 'detail' },
        [t.detail, t.eta ? `ETA ${duration(t.eta)}` : null].filter(Boolean).join(' · ')),
      el('div', { class: `bar ${t.progress < 0 ? 'indeterminate' : ''}` },
        el('i', { style: `width:${Math.max(0, t.progress * 100).toFixed(1)}%` })),
    );
  }
  return panel;
}

/* ---------- dashboard ---------- */

function viewDashboard() {
  const v = $('#view');
  v.replaceChildren();
  if (!STATUS) { v.append(el('div', { class: 'empty' }, 'Loading…')); return; }

  v.append(buildLivePanel());

  const c = STATUS.counts || {};
  const stats = [
    ['', STATUS.total || 0, 'Files tracked'],
    ['ok', c.ok || 0, 'Playable'],
    ['bad', c.corrupt || 0, 'Corrupt'],
    ['warn', c.incompatible || 0, 'Incompatible'],
    ['info', c.hygiene || 0, 'Needs tidying'],
    ['', (c.unknown || 0) + (c.error || 0), 'Not checked'],
  ];
  v.append(el('div', { class: 'grid grid-stats' },
    stats.map(([kind, n, label]) =>
      el('div', {
        class: `stat ${kind}`, style: 'cursor:pointer',
        onclick: () => {
          const map = { Corrupt: 'corrupt', Incompatible: 'incompatible', 'Needs tidying': 'hygiene', Playable: 'ok' };
          location.hash = `#/files${map[label] ? `?status=${map[label]}` : ''}`;
        },
      },
        el('div', { class: 'n' }, n.toLocaleString()),
        el('div', { class: 'l' }, label)))));

  v.append(el('div', { class: 'grid grid-2' },
    buildLibrariesCard(), buildServicesCard()));

  v.append(buildRecentCard());
}

function buildLibrariesCard() {
  const card = el('div', { class: 'card' },
    el('div', { class: 'card-head' }, el('h3', {}, 'Libraries')));
  const libs = STATUS.libraries || [];
  if (!libs.length) {
    card.append(el('div', { class: 'empty' },
      'No libraries yet. Connect Sonarr or Radarr in Settings, then run a scan.'));
    return card;
  }
  const table = el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, 'Library'), el('th', {}, 'Files'), el('th', {}, 'Size'),
      el('th', {}, 'OK'), el('th', {}, 'Problems'), el('th', {}, ''))),
    el('tbody', {}, libs.map((l) => el('tr', {},
      el('td', {}, l.library),
      el('td', {}, String(l.total)),
      el('td', { class: 'muted' }, bytes(l.bytes)),
      el('td', { class: 'sev-info' }, String(l.ok)),
      el('td', {},
        l.corrupt ? el('span', { class: 'pill pill-corrupt', style: 'margin-right:4px' }, `${l.corrupt} corrupt`) : null,
        l.incompatible ? el('span', { class: 'pill pill-incompatible', style: 'margin-right:4px' }, `${l.incompatible} incompatible`) : null,
        l.hygiene ? el('span', { class: 'pill pill-hygiene', style: 'margin-right:4px' }, `${l.hygiene} tidy`) : null,
        l.missing ? el('span', { class: 'pill pill-missing', style: 'margin-right:4px' }, `${l.missing} replacing`) : null,
        (!l.corrupt && !l.incompatible && !l.hygiene && !l.missing)
          ? el('span', { class: 'muted small' }, 'none') : null),
      el('td', {}, el('button', {
        class: 'btn btn-sm',
        disabled: STATUS.state.scan.running,
        onclick: () => startScan(l.library),
      }, 'Scan'))))));
  card.append(el('div', { class: 'table-wrap' }, table));
  return card;
}

function buildServicesCard() {
  const card = el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('h3', {}, 'Connections'),
      el('button', { class: 'btn btn-sm', onclick: recheckServices }, 'Re-test')));
  const svcs = STATUS.state.services || {};
  for (const name of ['sonarr', 'radarr', 'emby']) {
    const info = svcs[name] || { configured: false };
    const cls = !info.configured ? '' : info.ok ? 'up' : 'down';
    card.append(el('div', { class: 'service' },
      el('span', { class: `dot ${cls}` }),
      el('span', { style: 'font-weight:600;text-transform:capitalize;min-width:70px' }, name),
      el('span', { class: 'muted small' },
        !info.configured ? 'not configured'
          : info.ok ? `${info.name || ''} ${info.version || ''}`.trim()
            : (info.error || 'unreachable'))));
  }
  const w = STATUS.state.watchers || [];
  card.append(el('div', { class: 'service' },
    el('span', { class: `dot ${w.length ? 'up' : ''}` }),
    el('span', { style: 'font-weight:600;min-width:70px' }, 'Watch'),
    el('span', { class: 'muted small' },
      w.length ? w.join(', ') : 'no watch folders set')));
  const pending = STATUS.watch_pending || [];
  if (pending.length) {
    card.append(el('div', { class: 'small muted', style: 'margin-top:8px' },
      `${pending.length} file(s) still settling: ${pending.map((p) => basename(p.path)).join(', ')}`));
  }
  const rb = STATUS.recycle || {};
  card.append(el('div', { class: 'small muted', style: 'margin-top:10px' },
    `Recycle bin: ${rb.count || 0} file(s), ${bytes(rb.bytes)} — `,
    el('a', { href: '#/recycle' }, 'review')));
  return card;
}

function buildRecentCard() {
  const card = el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('h3', {}, 'Recent activity'),
      el('a', { href: '#/activity', class: 'small' }, 'See all')),
    el('div', { id: 'recentBody', class: 'empty' }, 'Loading…'));
  api('/activity?limit=12').then((rows) => {
    const body = $('#recentBody');
    if (!body) return;
    if (!rows.length) { body.textContent = 'Nothing has happened yet.'; return; }
    body.replaceWith(el('div', { class: 'table-wrap', id: 'recentBody' },
      el('table', {}, el('tbody', {}, rows.map(activityRow)))));
  }).catch(() => { });
  return card;
}

function activityRow(r) {
  return el('tr', {},
    el('td', { class: 'muted small', style: 'white-space:nowrap' }, ago(r.ts)),
    el('td', {}, el('span', { class: `sev-${r.level === 'warn' ? 'warning' : r.level}` },
      r.event.replace(/_/g, ' '))),
    el('td', { class: 'path' }, r.path ? basename(r.path) : ''),
    el('td', { class: 'muted small' }, (r.detail || '').slice(0, 160)));
}

/* ---------- files ---------- */

let filesQuery = { status: 'all', library: 'all', q: '', offset: 0, limit: 100 };

function viewFiles() {
  const v = $('#view');
  v.replaceChildren();
  v.append(buildLivePanel());

  const params = new URLSearchParams((location.hash.split('?')[1] || ''));
  if (params.get('status')) filesQuery.status = params.get('status');

  const statusSel = el('select', { onchange: (e) => { filesQuery.status = e.target.value; filesQuery.offset = 0; loadFiles(); } },
    ...['all', 'corrupt', 'incompatible', 'hygiene', 'missing', 'error', 'unknown', 'ok']
      .map((s) => el('option', { value: s, selected: filesQuery.status === s },
        s === 'all' ? 'All statuses' : (STATUS_LABEL[s] || s))));

  const libs = ['all', ...(STATUS?.libraries || []).map((l) => l.library)];
  const libSel = el('select', { onchange: (e) => { filesQuery.library = e.target.value; filesQuery.offset = 0; loadFiles(); } },
    ...libs.map((l) => el('option', { value: l, selected: filesQuery.library === l },
      l === 'all' ? 'All libraries' : l)));

  let searchTimer;
  const search = el('input', {
    type: 'search', placeholder: 'Search title or path…', value: filesQuery.q,
    oninput: (e) => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { filesQuery.q = e.target.value; filesQuery.offset = 0; loadFiles(); }, 250);
    },
  });

  v.append(el('div', { class: 'toolbar' }, statusSel, libSel, search,
    el('button', { class: 'btn', onclick: loadFiles }, 'Refresh')));
  v.append(el('div', { class: 'card' }, el('div', { id: 'filesBody', class: 'empty' }, 'Loading…')));
  loadFiles();
}

async function loadFiles() {
  const body = $('#filesBody');
  if (!body) return;
  const p = new URLSearchParams({
    status: filesQuery.status, library: filesQuery.library,
    limit: filesQuery.limit, offset: filesQuery.offset,
  });
  if (filesQuery.q) p.set('q', filesQuery.q);
  try {
    const data = await api(`/files?${p}`);
    if (!data.files.length) {
      body.replaceWith(el('div', { id: 'filesBody', class: 'empty' },
        'No files match. Run a scan, or widen the filter.'));
      return;
    }
    const table = el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, 'Status'), el('th', {}, 'Title'), el('th', {}, 'Library'),
        el('th', {}, 'Size'), el('th', {}, 'Problem'), el('th', {}, 'Checked'))),
      el('tbody', {}, data.files.map((f) => {
        const findings = (f.last_result?.findings || []).filter((x) => x.severity !== 'info');
        return clickableRow(() => openDrawer(f.path),
          el('td', {}, statusPill(f.status)),
          el('td', {},
            el('div', {}, f.title || basename(f.path)),
            el('div', { class: 'path' }, f.path)),
          el('td', { class: 'muted small' }, f.library || '—'),
          el('td', { class: 'muted small' }, bytes(f.size)),
          el('td', { class: 'small' },
            findings.length
              ? el('span', { class: `sev-${findings[0].severity}` },
                clip(findings[0].detail, 90) + (findings.length > 1 ? ` (+${findings.length - 1} more)` : ''))
              : el('span', { class: 'muted' }, '—')),
          el('td', { class: 'muted small', style: 'white-space:nowrap' }, ago(f.last_checked)));
      })));

    const pager = el('div', { class: 'spread', style: 'margin-top:12px' },
      el('span', { class: 'muted small' },
        `Showing ${filesQuery.offset + 1}–${Math.min(filesQuery.offset + data.files.length, data.total)} of ${data.total}`),
      el('span', { class: 'row' },
        el('button', {
          class: 'btn btn-sm', disabled: filesQuery.offset === 0,
          onclick: () => { filesQuery.offset = Math.max(0, filesQuery.offset - filesQuery.limit); loadFiles(); },
        }, 'Previous'),
        el('button', {
          class: 'btn btn-sm',
          disabled: filesQuery.offset + filesQuery.limit >= data.total,
          onclick: () => { filesQuery.offset += filesQuery.limit; loadFiles(); },
        }, 'Next')));

    body.replaceWith(el('div', { id: 'filesBody' },
      el('div', { class: 'table-wrap' }, table), pager));
  } catch (err) {
    body.replaceWith(el('div', { id: 'filesBody', class: 'empty' }, `Could not load files: ${err.message}`));
  }
}

/* ---------- file drawer ---------- */

async function openDrawer(path) {
  $('#drawer').classList.remove('hidden');
  $('#scrim').classList.remove('hidden');
  $('#drawerTitle').textContent = basename(path);
  const body = $('#drawerBody');
  body.replaceChildren(el('div', { class: 'empty' }, 'Loading…'));

  let data;
  try {
    data = await api(`/files/detail?path=${encodeURIComponent(path)}`);
  } catch (err) {
    body.replaceChildren(el('div', { class: 'empty' }, err.message));
    return;
  }

  const f = data.file;
  const probe = f.probe || {};
  body.replaceChildren();

  body.append(el('div', { class: 'row', style: 'margin-bottom:12px' },
    statusPill(f.status),
    el('span', { class: 'muted small' }, `checked ${ago(f.last_checked)}`),
    !data.exists ? el('span', { class: 'pill pill-missing' }, 'not on disk') : null));

  body.append(el('div', { class: 'path', style: 'margin-bottom:14px' }, f.path));

  const findings = (f.last_result?.findings || []);
  if (findings.length) {
    body.append(el('h3', {}, 'Findings'));
    body.append(el('div', { class: 'card', style: 'padding:0' },
      el('table', {}, el('tbody', {}, findings.map((x) => el('tr', {},
        el('td', { style: 'width:1%' },
          el('span', { class: `pill pill-${x.severity === 'error' ? 'corrupt' : x.severity === 'warning' ? 'incompatible' : 'unknown'}` },
            x.category)),
        el('td', {},
          el('div', {}, x.detail),
          el('div', { class: 'muted small mono' }, x.code))))))));
  } else {
    body.append(el('div', { class: 'muted small', style: 'margin-bottom:14px' },
      f.last_checked ? 'No problems found.' : 'This file has not been checked yet.'));
  }

  if (probe.video || probe.audio) {
    body.append(el('h3', { style: 'margin-top:18px' }, 'Streams'));
    const rows = [];
    if (probe.container) rows.push(['Container', `${probe.container}${probe.faststart === false ? ' (not faststart)' : ''}`]);
    if (probe.duration) rows.push(['Duration', duration(probe.duration)]);
    if (probe.bitrate) rows.push(['Bitrate', `${(probe.bitrate / 1e6).toFixed(2)} Mbps`]);
    if (probe.video) {
      const v = probe.video;
      rows.push(['Video', `${v.codec} ${v.width}×${v.height} ${v.bit_depth}-bit ${v.pix_fmt} @ ${v.fps} fps`]);
    }
    (probe.audio || []).forEach((a, i) => rows.push([`Audio ${i + 1}`,
      `${a.codec} ${a.channels}ch ${a.language || 'no language tag'}${a.default ? ' (default)' : ''}`]));
    (probe.subtitles || []).forEach((s, i) => rows.push([`Subtitle ${i + 1}`,
      `${s.codec} ${s.language || 'no language tag'}${s.forced ? ' forced' : ''}${s.image ? ' image-based' : ''}`]));
    body.append(el('div', { class: 'table-wrap' },
      el('table', {}, el('tbody', {}, rows.map(([k, v]) => el('tr', {},
        el('td', { class: 'muted small', style: 'width:110px' }, k),
        el('td', { class: 'small' }, v)))))));
  }

  body.append(el('h3', { style: 'margin-top:18px' }, 'Actions'));
  const actions = el('div', { class: 'row' },
    el('button', { class: 'btn', onclick: () => fileAction(path, 'recheck') }, 'Re-check'),
    el('button', { class: 'btn', onclick: () => fileAction(path, 'repair') }, 'Remux / repair'),
    el('button', { class: 'btn', onclick: () => fileAction(path, 'transcode') }, 'Transcode'),
    el('button', {
      class: 'btn btn-danger',
      onclick: () => {
        if (!confirm(`Delete ${basename(path)} and ask the *arr for a replacement?\n\nThe file goes to the recycle bin first.`)) return;
        fileAction(path, 'redownload');
      },
    }, 'Delete & re-search'),
    el('button', { class: 'btn btn-ghost', onclick: () => fileAction(path, 'cancel') }, 'Cancel running job'));
  body.append(actions);

  if (data.jobs.length) {
    body.append(el('h3', { style: 'margin-top:18px' }, 'Job history'));
    body.append(el('div', { class: 'table-wrap' },
      el('table', {}, el('tbody', {}, data.jobs.map((j) => el('tr', {},
        el('td', { class: 'small' }, j.kind),
        el('td', {}, el('span', { class: `pill pill-${j.state === 'done' ? 'ok' : j.state === 'failed' ? 'corrupt' : 'unknown'}` }, j.state)),
        el('td', { class: 'muted small' }, j.message || j.error || ''),
        el('td', { class: 'muted small', style: 'white-space:nowrap' }, ago(j.created))))))));
  }
}

async function fileAction(path, action) {
  try {
    let res;
    if (action === 'recheck') {
      res = await api(`/files/recheck?path=${encodeURIComponent(path)}&act=false`, { method: 'POST' });
      toast(`Re-checked: ${STATUS_LABEL[res.result.status] || res.result.status}. Policy would ${res.decision.action}.`, 'ok');
    } else if (action === 'cancel') {
      res = await api(`/files/cancel?path=${encodeURIComponent(path)}`, { method: 'POST' });
      toast(res.cancelled ? 'Cancelling the running job.' : 'No job is running for that file.');
    } else {
      res = await api(`/files/action?path=${encodeURIComponent(path)}&action=${action}`, { method: 'POST' });
      toast(`${action}: ${res.message || (res.ok ? 'done' : 'failed')}`, res.ok ? 'ok' : 'bad');
    }
    openDrawer(path);
    scheduleRefresh(300);
  } catch (err) {
    toast(err.message, 'bad');
  }
}

function closeDrawer() {
  $('#drawer').classList.add('hidden');
  $('#scrim').classList.add('hidden');
}

/* ---------- activity ---------- */

function viewActivity() {
  const v = $('#view');
  v.replaceChildren(buildLivePanel());
  v.append(el('div', { class: 'toolbar' },
    el('select', { id: 'actLevel', onchange: loadActivity },
      ...['all', 'info', 'warn', 'error'].map((l) => el('option', { value: l }, l === 'all' ? 'All levels' : l))),
    el('select', { id: 'jobState', onchange: loadJobs },
      ...['all', 'running', 'queued', 'done', 'failed'].map((l) => el('option', { value: l }, l === 'all' ? 'All jobs' : l))),
    el('button', { class: 'btn', onclick: () => { loadActivity(); loadJobs(); } }, 'Refresh')));
  v.append(el('div', { class: 'card' },
    el('div', { class: 'card-head' }, el('h3', {}, 'Jobs')),
    el('div', { id: 'jobsBody', class: 'empty' }, 'Loading…')));
  v.append(el('div', { class: 'card' },
    el('div', { class: 'card-head' }, el('h3', {}, 'Log')),
    el('div', { id: 'actBody', class: 'empty' }, 'Loading…')));
  loadActivity();
  loadJobs();
}

async function loadActivity() {
  const level = $('#actLevel')?.value || 'all';
  const rows = await api(`/activity?limit=250&level=${level}`).catch(() => []);
  const body = $('#actBody');
  if (!body) return;
  body.replaceWith(rows.length
    ? el('div', { class: 'table-wrap', id: 'actBody' },
      el('table', {}, el('tbody', {}, rows.map(activityRow))))
    : el('div', { class: 'empty', id: 'actBody' }, 'Nothing logged yet.'));
}

async function loadJobs() {
  const st = $('#jobState')?.value || 'all';
  const rows = await api(`/jobs?state_filter=${st}&limit=100`).catch(() => []);
  const body = $('#jobsBody');
  if (!body) return;
  body.replaceWith(rows.length
    ? el('div', { class: 'table-wrap', id: 'jobsBody' },
      el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, 'Action'), el('th', {}, 'File'), el('th', {}, 'State'),
          el('th', {}, 'Progress'), el('th', {}, 'Message'), el('th', {}, 'When'))),
        el('tbody', {}, rows.map((j) => clickableRow(() => j.path && openDrawer(j.path),
          el('td', {}, j.kind),
          el('td', { class: 'small' }, basename(j.path) || '—'),
          el('td', {}, el('span', { class: `pill pill-${j.state === 'done' ? 'ok' : j.state === 'failed' ? 'corrupt' : 'unknown'}` }, j.state)),
          el('td', { class: 'muted small' }, j.state === 'running' ? `${Math.round((j.progress || 0) * 100)}%` : '—'),
          el('td', { class: 'muted small' }, (j.message || j.error || '').slice(0, 120)),
          el('td', { class: 'muted small', style: 'white-space:nowrap' }, ago(j.created)))))))
    : el('div', { class: 'empty', id: 'jobsBody' }, 'No jobs yet.'));
}

/* ---------- recycle bin ---------- */

async function viewRecycle() {
  const v = $('#view');
  v.replaceChildren(buildLivePanel());
  const card = el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('h3', {}, 'Recycle bin'),
      el('button', {
        class: 'btn btn-danger btn-sm',
        onclick: async () => {
          if (!confirm('Permanently delete everything in the recycle bin?')) return;
          const r = await api('/recycle/empty', { method: 'POST' }).catch((e) => { toast(e.message, 'bad'); });
          if (r) { toast(`Removed ${r.removed} file(s).`, 'ok'); viewRecycle(); }
        },
      }, 'Empty now')),
    el('div', { class: 'muted small', style: 'margin-bottom:12px' },
      'Files deleted by unfuckarr are kept here until the retention window in Settings expires, so an automatic decision can be undone.'),
    el('div', { id: 'recBody', class: 'empty' }, 'Loading…'));
  v.append(card);

  const rows = await api('/recycle?limit=300').catch(() => []);
  const body = $('#recBody');
  if (!body) return;
  body.replaceWith(rows.length
    ? el('div', { class: 'table-wrap', id: 'recBody' },
      el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, 'File'), el('th', {}, 'Size'), el('th', {}, 'Reason'),
          el('th', {}, 'Deleted'), el('th', {}, ''))),
        el('tbody', {}, rows.map((r) => el('tr', {},
          el('td', {}, el('div', {}, basename(r.original)), el('div', { class: 'path' }, r.original)),
          el('td', { class: 'muted small' }, bytes(r.size)),
          el('td', { class: 'muted small' }, r.reason || ''),
          el('td', { class: 'muted small', style: 'white-space:nowrap' }, when(r.deleted)),
          el('td', {}, r.stored ? el('button', {
            class: 'btn btn-sm',
            onclick: async () => {
              try {
                const out = await api(`/recycle/restore?id=${r.id}`, { method: 'POST' });
                toast(`Restored to ${out.restored}`, 'ok');
                viewRecycle();
              } catch (e) { toast(e.message, 'bad'); }
            },
          }, 'Restore') : el('span', { class: 'muted small' }, 'not kept')))))))
    : el('div', { class: 'empty', id: 'recBody' }, 'The recycle bin is empty.'));
}

/* ---------- settings ---------- */

const FIELD_HELP = {
  'integrity.depth': 'quick = ffprobe only (seconds per file). sample = decode the start, middle and end. full = decode every frame; correct but very slow.',
  'integrity.duration_tolerance_pct': 'A file more than this much shorter than the runtime Sonarr/Radarr expects is reported — but only reported. That runtime is nominal (for TV it is the broadcast slot, ad breaks included), so a normal episode sits well under it.',
  'integrity.duration_truncated_pct': 'A file this much shorter than its expected runtime is treated as truncated, and the corrupt action applies. Keep it high: a 44 min drama in a 60 min slot is 27% short and perfectly healthy.',
  'emby_compat.target_profile': 'modern = a current Emby app (HEVC, 10-bit). conservative = Chromecast-class hardware (H.264 8-bit only). permissive = only reject what nothing plays.',
  'policy.corrupt_action': 'What to do with a file that is genuinely broken.',
  'policy.incompatible_action': 'What to do with an intact file Emby would have to transcode.',
  'policy.hygiene_action': 'What to do about missing language tags, default-track flags and similar. These never justify deleting a file.',
  'policy.recycle_bin_days': 'Deleted files are kept this long so an automatic decision can be undone. 0 deletes immediately.',
  'policy.max_actions_per_scan': 'Hard cap on how many files one scan may transcode or delete.',
  'policy.abort_if_failure_ratio_over': 'If more than this fraction of the library fails a scan, stop and change nothing — that is what an unmounted array looks like.',
  'transcode.hwaccel': 'qsv needs /dev/dri passed into the container; nvenc needs the NVIDIA runtime; vaapi needs /dev/dri and the right render node.',
  'transcode.replace_original': 'On: the transcode replaces the file in place (original goes to the recycle bin) and the *arr is told to rescan. Off: the new file is left alongside the old one.',
  'schedule.recheck_after_days': 'Re-verify a file that passed, even if nothing about it has changed. Catches silent corruption on the array.',
};

function settingField(pathKey, value, opts = {}) {
  const label = opts.label || pathKey.split('.').pop().replace(/_/g, ' ')
    .replace(/^./, (c) => c.toUpperCase());
  const hint = FIELD_HELP[pathKey];
  let input;
  if (typeof value === 'boolean') {
    input = el('input', { type: 'checkbox', 'data-path': pathKey });
    input.checked = value;
    return el('div', { class: 'field' },
      el('div', { class: 'field-inline' }, input, el('label', {}, label)),
      hint ? el('div', { class: 'hint' }, hint) : null);
  }
  if (opts.options) {
    input = el('select', { 'data-path': pathKey },
      ...opts.options.map((o) => el('option', { value: o, selected: value === o }, o)));
  } else if (typeof value === 'number') {
    input = el('input', {
      type: 'number', 'data-path': pathKey, value: String(value),
      step: Number.isInteger(value) ? '1' : 'any',
    });
  } else if (opts.list) {
    input = el('textarea', { 'data-path': pathKey, rows: 3, 'data-list': '1' },
      (value || []).join('\n'));
  } else {
    input = el('input', {
      type: opts.password ? 'password' : 'text', 'data-path': pathKey,
      value: value ?? '', placeholder: opts.placeholder || '',
    });
  }
  return el('div', { class: 'field' },
    el('label', {}, label), input,
    hint ? el('div', { class: 'hint' }, hint) : null);
}

async function viewSettings() {
  const v = $('#view');
  v.replaceChildren(el('div', { class: 'empty' }, 'Loading settings…'));
  try {
    SETTINGS = await api('/settings');
  } catch (err) {
    v.replaceChildren(el('div', { class: 'empty' }, `Could not load settings: ${err.message}`));
    return;
  }
  const s = SETTINGS;
  v.replaceChildren();

  // Services
  for (const [key, title, port] of [['sonarr', 'Sonarr', 8989], ['radarr', 'Radarr', 7878]]) {
    v.append(el('div', { class: 'card' },
      el('div', { class: 'card-head' }, el('h3', {}, title),
        el('button', { class: 'btn btn-sm', onclick: () => testConn(key) }, 'Test')),
      settingField(`${key}.enabled`, s[key].enabled, { label: `Use ${title}` }),
      el('div', { class: 'cols' },
        settingField(`${key}.url`, s[key].url, { label: 'URL', placeholder: `http://192.168.1.10:${port}` }),
        settingField(`${key}.api_key`, s[key].api_key, { label: 'API key', password: true })),
      pathMappingEditor(key, s[key].path_mappings),
      el('div', { id: `test-${key}`, class: 'small muted', style: 'margin-top:8px' })));
  }

  v.append(el('div', { class: 'card' },
    el('div', { class: 'card-head' }, el('h3', {}, 'Emby'),
      el('button', { class: 'btn btn-sm', onclick: () => testConn('emby') }, 'Test')),
    settingField('emby.enabled', s.emby.enabled, { label: 'Use Emby' }),
    el('div', { class: 'cols' },
      settingField('emby.url', s.emby.url, { label: 'URL', placeholder: 'http://192.168.1.10:8096' }),
      settingField('emby.api_key', s.emby.api_key, { label: 'API key', password: true })),
    settingField('emby.use_playback_info', s.emby.use_playback_info,
      { label: 'Ask Emby whether it would direct play each file' }),
    el('div', { class: 'hint', style: 'margin:-6px 0 12px' },
      'Authoritative: Emby answers using its own version and codec support, and says why it would transcode. Costs one request per file.'),
    settingField('emby.read_activity_log', s.emby.read_activity_log,
      { label: "Read Emby's activity log for real playback failures" }),
    pathMappingEditor('emby', s.emby.path_mappings),
    el('div', { id: 'test-emby', class: 'small muted', style: 'margin-top:8px' })));

  // Watch folders
  v.append(watchFolderCard(s.watch_folders));

  v.append(el('div', { class: 'card' },
    el('h3', {}, 'Extra library paths'),
    el('div', { class: 'hint', style: 'margin-bottom:8px' },
      'Folders to sweep that no *arr knows about. One per line. Files found here can be transcoded but not re-downloaded.'),
    settingField('extra_library_paths', s.extra_library_paths, { list: true, label: 'Paths' })));

  // Checks
  v.append(el('div', { class: 'card' },
    el('h3', {}, 'Integrity checks'),
    settingField('integrity.enabled', s.integrity.enabled, { label: 'Check files are intact' }),
    el('div', { class: 'cols' },
      settingField('integrity.depth', s.integrity.depth, { options: ['quick', 'sample', 'full'] }),
      settingField('integrity.sample_seconds', s.integrity.sample_seconds),
      settingField('integrity.duration_tolerance_pct', s.integrity.duration_tolerance_pct),
      settingField('integrity.duration_truncated_pct', s.integrity.duration_truncated_pct),
      settingField('integrity.min_duration_seconds', s.integrity.min_duration_seconds),
      settingField('integrity.max_decode_errors', s.integrity.max_decode_errors)),
    settingField('integrity.fail_on_missing_audio', s.integrity.fail_on_missing_audio)));

  v.append(el('div', { class: 'card' },
    el('h3', {}, 'Emby compatibility'),
    settingField('emby_compat.enabled', s.emby_compat.enabled, { label: 'Check files direct play' }),
    settingField('emby_compat.target_profile', s.emby_compat.target_profile,
      { options: ['modern', 'conservative', 'permissive', 'custom'] }),
    el('div', { class: 'cols' },
      settingField('emby_compat.video_codecs', s.emby_compat.video_codecs, { list: true }),
      settingField('emby_compat.audio_codecs', s.emby_compat.audio_codecs, { list: true }),
      settingField('emby_compat.containers', s.emby_compat.containers, { list: true })),
    el('div', { class: 'hint', style: 'margin:-4px 0 12px' },
      'The three lists above apply only when the profile is "custom".'),
    el('div', { class: 'cols' },
      settingField('emby_compat.max_height', s.emby_compat.max_height)),
    settingField('emby_compat.allow_10bit', s.emby_compat.allow_10bit),
    settingField('emby_compat.require_faststart_mp4', s.emby_compat.require_faststart_mp4)));

  v.append(el('div', { class: 'card' },
    el('h3', {}, 'Stream hygiene'),
    settingField('hygiene.enabled', s.hygiene.enabled, { label: 'Check stream metadata' }),
    settingField('hygiene.require_audio_language_tags', s.hygiene.require_audio_language_tags),
    settingField('hygiene.require_default_audio_track', s.hygiene.require_default_audio_track),
    settingField('hygiene.flag_image_subtitles_only', s.hygiene.flag_image_subtitles_only),
    settingField('hygiene.flag_missing_subtitle_language', s.hygiene.flag_missing_subtitle_language),
    el('div', { class: 'cols' },
      settingField('hygiene.min_fps', s.hygiene.min_fps),
      settingField('hygiene.max_fps', s.hygiene.max_fps))));

  // Policy
  v.append(el('div', { class: 'card' },
    el('h3', {}, 'What to do about it'),
    el('div', { class: 'hint', style: 'margin-bottom:12px' },
      'These run unattended. Deleted files go to the recycle bin first, so a wrong call can be undone until retention expires.'),
    el('div', { class: 'cols' },
      settingField('policy.corrupt_action', s.policy.corrupt_action,
        { options: ['none', 'flag', 'transcode', 'redownload'] }),
      settingField('policy.incompatible_action', s.policy.incompatible_action,
        { options: ['none', 'flag', 'transcode', 'redownload'] }),
      settingField('policy.hygiene_action', s.policy.hygiene_action,
        { options: ['none', 'flag', 'transcode'] })),
    settingField('policy.try_repair_before_redownload', s.policy.try_repair_before_redownload,
      { label: 'Try a remux before re-downloading a corrupt file' }),
    settingField('policy.blocklist_on_redownload', s.policy.blocklist_on_redownload,
      { label: 'Blocklist the release so the same bad file is not grabbed again' }),
    el('div', { class: 'cols' },
      settingField('policy.recycle_bin_days', s.policy.recycle_bin_days),
      settingField('policy.recycle_bin_path', s.policy.recycle_bin_path,
        { label: 'Recycle bin path', placeholder: '/config/recycle' }),
      settingField('policy.max_actions_per_scan', s.policy.max_actions_per_scan),
      settingField('policy.abort_if_failure_ratio_over', s.policy.abort_if_failure_ratio_over))));

  // Transcoding
  v.append(el('div', { class: 'card' },
    el('h3', {}, 'Transcoding'),
    settingField('transcode.enabled', s.transcode.enabled),
    el('div', { class: 'cols' },
      settingField('transcode.video_codec', s.transcode.video_codec, { options: ['h264', 'hevc', 'copy'] }),
      settingField('transcode.hwaccel', s.transcode.hwaccel,
        { options: ['none', 'qsv', 'nvenc', 'vaapi', 'videotoolbox'] }),
      settingField('transcode.vaapi_device', s.transcode.vaapi_device),
      settingField('transcode.crf', s.transcode.crf),
      settingField('transcode.preset', s.transcode.preset),
      settingField('transcode.audio_codec', s.transcode.audio_codec, { options: ['aac', 'ac3', 'eac3', 'copy'] }),
      settingField('transcode.audio_bitrate', s.transcode.audio_bitrate),
      settingField('transcode.container', s.transcode.container, { options: ['mkv', 'mp4'] }),
      settingField('transcode.max_concurrent', s.transcode.max_concurrent),
      settingField('transcode.nice_level', s.transcode.nice_level),
      settingField('transcode.stall_timeout_seconds', s.transcode.stall_timeout_seconds)),
    settingField('transcode.copy_compatible_streams', s.transcode.copy_compatible_streams,
      { label: 'Copy streams that already pass, instead of re-encoding them' }),
    settingField('transcode.keep_subtitles', s.transcode.keep_subtitles),
    settingField('transcode.replace_original', s.transcode.replace_original)));

  // Schedule
  v.append(el('div', { class: 'card' },
    el('h3', {}, 'Schedule'),
    settingField('schedule.scan_enabled', s.schedule.scan_enabled, { label: 'Scan on a schedule' }),
    settingField('schedule.scan_at_startup', s.schedule.scan_at_startup),
    el('div', { class: 'cols' },
      settingField('schedule.scan_interval_hours', s.schedule.scan_interval_hours),
      settingField('schedule.recheck_after_days', s.schedule.recheck_after_days),
      settingField('schedule.max_concurrent_probes', s.schedule.max_concurrent_probes)),
    settingField('schedule.skip_unchanged', s.schedule.skip_unchanged,
      { label: 'Skip files that have not changed since their last clean check' })));

  v.append(el('div', { class: 'card' },
    el('h3', {}, 'Advanced'),
    el('div', { class: 'cols' },
      settingField('ffmpeg_path', s.ffmpeg_path),
      settingField('ffprobe_path', s.ffprobe_path),
      settingField('log_level', s.log_level, { options: ['DEBUG', 'INFO', 'WARNING', 'ERROR'] }),
      settingField('api_key', s.api_key, { label: 'Web API key (blank = no auth)', password: true }))));

  v.append(el('div', { class: 'sticky-save' },
    el('span', { id: 'saveMsg', class: 'muted small' }),
    el('button', { class: 'btn', onclick: viewSettings }, 'Discard changes'),
    el('button', { class: 'btn btn-primary', onclick: saveSettings }, 'Save')));
}

function pathMappingEditor(key, mappings) {
  const wrap = el('div', { class: 'field' },
    el('label', {}, 'Path mappings'),
    el('div', { class: 'hint', style: 'margin:0 0 6px' },
      `If ${key} reports a path this container cannot see, map it. One per line, as "remote = local", e.g. /tv = /media/tv`),
    el('textarea', {
      'data-path': `${key}.path_mappings`, 'data-mappings': '1', rows: 2,
      placeholder: '/tv = /media/tv',
    }, (mappings || []).map((m) => `${m.from} = ${m.to}`).join('\n')));
  return wrap;
}

function watchFolderCard(folders) {
  const list = el('div', { id: 'watchList' });
  const render = () => {
    list.replaceChildren();
    if (!folders.length) {
      list.append(el('div', { class: 'muted small', style: 'padding:8px 0' },
        'No watch folders. Add one to have new downloads checked the moment they land, before Emby ever sees them.'));
    }
    folders.forEach((f, i) => {
      list.append(el('div', { class: 'row', style: 'margin-bottom:8px' },
        el('input', {
          type: 'text', value: f.path, placeholder: '/media/downloads/complete',
          style: 'flex:1;min-width:200px',
          oninput: (e) => { folders[i].path = e.target.value; },
        }),
        el('label', { class: 'row small muted', style: 'gap:5px' },
          (() => {
            const c = el('input', { type: 'checkbox', onchange: (e) => { folders[i].enabled = e.target.checked; } });
            c.checked = f.enabled; return c;
          })(), 'enabled'),
        el('label', { class: 'row small muted', style: 'gap:5px' },
          (() => {
            const c = el('input', { type: 'checkbox', onchange: (e) => { folders[i].recursive = e.target.checked; } });
            c.checked = f.recursive; return c;
          })(), 'recursive'),
        el('input', {
          type: 'number', value: String(f.settle_seconds), style: 'width:90px',
          title: 'Seconds the file must stop changing before it is checked',
          oninput: (e) => { folders[i].settle_seconds = Number(e.target.value) || 0; },
        }),
        el('span', { class: 'muted small' }, 's settle'),
        el('button', { class: 'btn btn-sm', onclick: () => browseFor((p) => { folders[i].path = p; render(); }) }, 'Browse'),
        el('button', { class: 'btn btn-sm btn-danger', onclick: () => { folders.splice(i, 1); render(); } }, 'Remove')));
    });
  };
  render();
  SETTINGS.watch_folders = folders;
  return el('div', { class: 'card' },
    el('div', { class: 'card-head' }, el('h3', {}, 'Watch folders'),
      el('button', {
        class: 'btn btn-sm',
        onclick: () => { folders.push({ path: '', enabled: true, settle_seconds: 60, recursive: true }); render(); },
      }, 'Add folder')),
    el('div', { class: 'hint', style: 'margin-bottom:10px' },
      'A new or changed video file here is checked as soon as it stops being written to, and the same policy applies. This is the live import gate; scheduled scans still cover the whole library.'),
    list);
}

async function browseFor(onPick) {
  let current = '/';
  const body = el('div', { class: 'drawer-body' });
  $('#drawerTitle').textContent = 'Choose a folder';
  $('#drawerBody').replaceWith(body);
  body.id = 'drawerBody';
  $('#drawer').classList.remove('hidden');
  $('#scrim').classList.remove('hidden');

  const load = async (path) => {
    body.replaceChildren(el('div', { class: 'empty' }, 'Loading…'));
    let data;
    try {
      data = await api(`/browse?path=${encodeURIComponent(path)}`);
    } catch (err) {
      body.replaceChildren(el('div', { class: 'empty' }, err.message),
        el('button', { class: 'btn', onclick: () => load('/') }, 'Back to /'));
      return;
    }
    current = data.path;
    body.replaceChildren(
      el('div', { class: 'mono', style: 'margin-bottom:10px;word-break:break-all' }, current),
      el('div', { class: 'row', style: 'margin-bottom:12px' },
        data.parent ? el('button', { class: 'btn btn-sm', onclick: () => load(data.parent) }, '⬆ Up') : null,
        el('button', {
          class: 'btn btn-sm btn-primary',
          onclick: () => { onPick(current); closeDrawer(); },
        }, 'Use this folder')),
      data.directories.length
        ? el('div', { class: 'table-wrap' }, el('table', {}, el('tbody', {},
          data.directories.map((d) => el('tr', { class: 'click', onclick: () => load(d.path) },
            el('td', {}, '📁 ' + d.name))))))
        : el('div', { class: 'empty' }, 'No sub-folders here.'));
  };
  load(current);
}

function collectSettings() {
  const out = JSON.parse(JSON.stringify(SETTINGS));
  for (const input of $$('[data-path]')) {
    const path = input.dataset.path.split('.');
    let value;
    if (input.dataset.mappings) {
      value = input.value.split('\n').map((line) => {
        const [from, ...rest] = line.split('=');
        return { from: (from || '').trim(), to: rest.join('=').trim() };
      }).filter((m) => m.from && m.to);
    } else if (input.dataset.list) {
      value = input.value.split('\n').map((x) => x.trim()).filter(Boolean);
    } else if (input.type === 'checkbox') {
      value = input.checked;
    } else if (input.type === 'number') {
      value = input.value === '' ? 0 : Number(input.value);
    } else {
      value = input.value;
    }
    let node = out;
    for (const k of path.slice(0, -1)) node = node[k];
    node[path.at(-1)] = value;
  }
  out.watch_folders = (SETTINGS.watch_folders || []).filter((f) => f.path.trim());
  return out;
}

async function saveSettings() {
  const msg = $('#saveMsg');
  msg.textContent = 'Saving…';
  try {
    SETTINGS = await api('/settings', { method: 'PUT', body: JSON.stringify(collectSettings()) });
    msg.textContent = 'Saved.';
    toast('Settings saved. Watch folders and schedule reloaded.', 'ok');
    refreshStatus();
    setTimeout(() => { msg.textContent = ''; }, 4000);
  } catch (err) {
    msg.textContent = '';
    toast(`Could not save: ${err.message}`, 'bad');
  }
}

async function testConn(key) {
  const target = $(`#test-${key}`);
  target.textContent = 'Testing…';
  const payload = collectSettings()[key];
  try {
    const res = await api(`/settings/test?service_name=${key}`, {
      method: 'POST', body: JSON.stringify(payload),
    });
    if (res.ok === false) {
      target.className = 'small sev-error';
      target.textContent = res.error;
    } else {
      target.className = 'small sev-info';
      target.textContent = res.root_folders
        ? `Connected to ${res.name} ${res.version}. Root folders: ${res.root_folders.join(', ') || 'none'}`
        : `Connected to ${res.name} ${res.version}. ${res.items_indexed ?? 0} items indexed.`;
    }
  } catch (err) {
    target.className = 'small sev-error';
    target.textContent = err.message;
  }
}

async function recheckServices() {
  try {
    await api('/services');
    toast('Connections re-tested.', 'ok');
    refreshStatus();
  } catch (err) { toast(err.message, 'bad'); }
}

/* ---------- routing ---------- */

const ROUTES = {
  '/': viewDashboard,
  '/files': viewFiles,
  '/activity': viewActivity,
  '/recycle': viewRecycle,
  '/settings': viewSettings,
};

function render() {
  (ROUTES[ROUTE] || viewDashboard)();
}

function route() {
  ROUTE = (location.hash.replace(/^#/, '').split('?')[0]) || '/';
  $$('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.route === ROUTE));
  closeDrawer();
  render();
}

async function startScan(library) {
  try {
    await api(`/scan/start${library && library !== 'all' ? `?library=${encodeURIComponent(library)}` : ''}`,
      { method: 'POST' });
    toast('Scan started.', 'ok');
    refreshStatus();
  } catch (err) { toast(err.message, 'bad'); }
}

/* ---------- boot ---------- */

$('#scanBtn').addEventListener('click', () => {
  if (STATUS?.state.scan.running) return;
  startScan();
});
$('#pauseBtn').addEventListener('click', async () => {
  const next = !STATUS.state.paused;
  await api(`/pause?paused=${next}`, { method: 'POST' }).catch((e) => toast(e.message, 'bad'));
  refreshStatus();
});
$('#drawerClose').addEventListener('click', closeDrawer);
$('#scrim').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
window.addEventListener('hashchange', route);

refreshStatus().then(route);
connectEvents();
// The event stream carries everything that changes quickly; this is the
// backstop for anything that does not emit an event.
setInterval(refreshStatus, 30000);
