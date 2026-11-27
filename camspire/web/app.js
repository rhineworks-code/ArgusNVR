// CamSpire NVR – Web-Oberfläche (ohne Build-Schritt, reines ES-Modul)
import { settingsPage } from './settings.js';
import { historyPage } from './history.js';
import { initAccount, logout, registerSW, renderAuthScreen } from './account.js';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const main = $('#main');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const ICON = {
  person: '<svg viewBox="0 0 24 24"><circle cx="12" cy="7" r="4"/><path d="M5 21c1-4 4-6 7-6s6 2 7 6"/></svg>',
  vehicle: '<svg viewBox="0 0 24 24"><path d="M5 17h14v-5l-2-5H7l-2 5zM5 12h14"/><circle cx="8" cy="17" r="2"/><circle cx="16" cy="17" r="2"/></svg>',
  animal: '<svg viewBox="0 0 24 24"><circle cx="5" cy="10" r="2"/><circle cx="9" cy="5" r="2"/><circle cx="15" cy="5" r="2"/><circle cx="19" cy="10" r="2"/><path d="M12 11c-3 0-6 4-6 7 0 2 2 3 6 3s6-1 6-3c0-3-3-7-6-7z"/></svg>',
  ai: '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><rect x="7" y="9" width="6" height="6"/></svg>',
  expand: '<svg viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>',
  close: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>',
  upload: '<svg viewBox="0 0 24 24"><path d="M12 16V4M6 10l6-6 6 6M4 20h16"/></svg>',
  empty: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 15s1.5-2 4-2 4 2 4 2M9 9h.01M15 9h.01"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  sparkle: '<svg viewBox="0 0 24 24"><path d="M12 3l2 5 5 2-5 2-2 5-2-5-5-2 5-2z"/></svg>',
};
const GROUP = { person: 'Person', vehicle: 'Fahrzeug', animal: 'Tier' };
const LABEL = { person: 'Person', car: 'Auto', truck: 'LKW', bus: 'Bus', motorcycle: 'Motorrad', bicycle: 'Fahrrad',
  dog: 'Hund', cat: 'Katze', bird: 'Vogel', horse: 'Pferd', sheep: 'Schaf', cow: 'Kuh', bear: 'Bär', boat: 'Boot' };

// ---------------------------------------------------------------- API
async function api(path, opts = {}) {
  const o = { ...opts, headers: { ...(opts.headers || {}) } };
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers['Content-Type'] = 'application/json'; delete o.json; }
  const r = await fetch(path, o);
  if (r.status === 401 && !path.startsWith('/api/auth/') && !document.body.classList.contains('auth-mode')) {
    // Sitzung abgelaufen -> Anmeldung zeigen
    initAccount(helpers());
    renderAuthScreen(await (await fetch('/api/auth/status')).json());
  }
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
  return r.headers.get('content-type')?.includes('json') ? r.json() : r;
}

const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const fmtDay = (ts) => {
  const d = new Date(ts * 1000), t = new Date(); const y = new Date(); y.setDate(t.getDate() - 1);
  if (d.toDateString() === t.toDateString()) return 'Heute';
  if (d.toDateString() === y.toDateString()) return 'Gestern';
  return d.toLocaleDateString('de-DE', { weekday: 'long', day: '2-digit', month: 'long' });
};
const fmtAgo = (ts) => {
  if (!ts) return '–'; const s = Date.now() / 1000 - ts;
  if (s < 60) return 'gerade eben'; if (s < 3600) return `vor ${Math.floor(s / 60)} min`;
  if (s < 86400) return `vor ${Math.floor(s / 3600)} h`; return `vor ${Math.floor(s / 86400)} Tagen`;
};
const fmtDur = (a, b) => { if (!b) return 'läuft'; const s = Math.max(1, Math.round(b - a)); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`; };
const fmtBytes = (b) => { if (!b) return '0 B'; const u = ['B', 'KB', 'MB', 'GB', 'TB']; const i = Math.min(4, Math.floor(Math.log(b) / Math.log(1024))); return `${(b / 1024 ** i).toFixed(i > 1 ? 1 : 0)} ${u[i]}`; };
const fmtUp = (s) => { const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return d ? `${d} T ${h} h` : h ? `${h} h ${m} min` : `${m} min`; };

function toast(html, onclick) {
  const t = document.createElement('div'); t.className = 'toast'; t.innerHTML = html;
  if (onclick) t.onclick = () => { onclick(); t.remove(); };
  $('#toasts').append(t); setTimeout(() => t.remove(), 6000);
}

// ---------------------------------------------------------------- State
const state = { cams: [], counts: {}, stats: null, persons: [], page: null, eventsFilter: { camera: '', grp: '', person: '' } };
const listeners = new Set();
const faceToasted = new Set();
const emit = (msg) => listeners.forEach((fn) => fn(msg));

// ---------------------------------------------------------------- WebSocket
function connectWS() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/ws`);
  ws.onopen = () => { $('#conn').classList.add('on'); $('#conn span').textContent = 'verbunden'; };
  ws.onclose = () => { $('#conn').classList.remove('on'); $('#conn span').textContent = 'getrennt'; setTimeout(connectWS, 2500); };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === 'counts') state.counts[m.camera] = m.counts;
    if (m.type === 'codecs') { const c = state.cams.find((x) => x.id === m.camera); if (c) c.codecs = m.codecs; }
    if (m.type === 'stats') { state.stats = m.stats; const n = m.stats.active_events; const b = $('#nav-active'); b.hidden = !n; b.textContent = n; }
    if (m.type === 'event' && m.action === 'new') {
      const ev = m.event; const cam = state.cams.find((c) => c.id === ev.camera);
      toast(`<span class="c-${ev.grp}">${ICON[ev.grp] || ''}</span><div><b>${esc(LABEL[ev.label] || ev.label)}</b><div style="color:var(--muted);font-size:12px">${esc(cam?.name || ev.camera)} · ${fmtTime(ev.start_ts)}</div></div>`,
        () => (location.hash = '#/events'));
    }
    if (m.type === 'event' && m.action === 'update' && m.event.sub_label && !faceToasted.has(m.event.id)) {
      faceToasted.add(m.event.id);
      toast(`${ICON.sparkle}<div><b class="who">${esc(m.event.sub_label)}</b> erkannt<div style="color:var(--muted);font-size:12px">${esc(m.event.camera)}</div></div>`);
    }
    emit(m);
  };
}

// ---------------------------------------------------------------- Live-Player (go2rtc)
let VideoRTC = null;
async function loadPlayer() {
  if (VideoRTC) return VideoRTC;
  try {
    const mod = await import('/go2rtc/video-rtc.js');
    VideoRTC = mod.VideoRTC;
    class CamSpireVideo extends VideoRTC {
      oninit() { super.oninit(); this.video.muted = true; this.video.playsInline = true; this.video.controls = false; }
      onconnect() { const r = super.onconnect(); return r; }
    }
    if (!customElements.get('camspire-video')) customElements.define('camspire-video', CamSpireVideo);
  } catch (e) { console.warn('go2rtc Player nicht verfügbar', e); VideoRTC = false; }
  return VideoRTC;
}

async function playHls(video, url, offset = 0) {
  if (!window.Hls) await new Promise((r) => { const sc = document.createElement('script'); sc.src = '/static/vendor/hls.light.min.js'; sc.onload = r; sc.onerror = r; document.head.append(sc); });
  if (window.Hls && window.Hls.isSupported()) {
    const h = new window.Hls({ startPosition: offset });
    h.loadSource(url); h.attachMedia(video);
    video._hls = h;
  } else { video.src = url; video.addEventListener('loadedmetadata', () => { video.currentTime = offset; }, { once: true }); }
}

function wsUrl(stream) { return `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/go2rtc/api/ws?src=${encodeURIComponent(stream)}`; }

const HEVC_OK = (() => {
  try { return !!window.MediaSource && ['hvc1.1.6.L120.90', 'hev1.1.6.L120.90', 'hvc1.1.6.L93.B0'].some((c) => MediaSource.isTypeSupported(`video/mp4; codecs="${c}"`)); }
  catch { return false; }
})();
// Reihenfolge der Versuche: vorgewärmter Schnellstart-Stream (Raster) bzw. Originalstream (Vollbild),
// danach die umgewandelte H.264-Variante
function streamCandidates(cam, big) {
  const native = big ? cam.main_stream : cam.live_stream;
  const h264 = big ? cam.main_stream_h264 : cam.live_stream_h264;
  const codec = (big ? cam.codecs?.main : (cam.codecs?.sub || cam.codecs?.main))?.codec || '';
  const hevcBad = /hevc|h265/i.test(codec) && !HEVC_OK;
  const list = [];
  if (!big && cam.live_fast) list.push(cam.live_fast);
  if (cam.transcode === 'on' || hevcBad) { if (h264) list.push(h264); }
  else { list.push(native); if (h264) list.push(h264); }
  return [...new Set(list.filter(Boolean))];
}

export function camTile(cam, { big = false } = {}) {
  const el = document.createElement('div');
  el.className = 'cam'; el.dataset.cam = cam.id;
  el.innerHTML = `
    <div class="view-wrap" style="width:100%;height:100%"></div>
    <div class="hud">
      <div class="row"><i class="dot ${cam.stats?.status || ''}"></i><span class="name">${esc(cam.name)}</span>
        <div class="tools">
          <button class="ai" title="KI-Ansicht mit Erkennungsrahmen">${ICON.ai}<span>KI</span></button>
          ${big ? '' : `<button class="open" title="Groß anzeigen">${ICON.expand}</button>`}
        </div></div>
      <div class="row counts"></div>
    </div>`;
  const wrap = $('.view-wrap', el);
  let mode = 'live', timer = null, player = null, watchdog = null, posterTimer = null;
  const still = (w) => `/api/cameras/${cam.id}/frame.jpg?w=${w}&t=${Date.now()}`;
  const showLive = async (attempt = 0) => {
    clearInterval(timer); clearTimeout(watchdog); clearInterval(posterTimer); wrap.innerHTML = ''; el.classList.remove('ready');
    // Sofort ein aktuelles Bild aus der Erkennung zeigen und laufend erneuern, bis das Video da ist
    const poster = document.createElement('img');
    poster.className = 'view poster'; poster.alt = '';
    wrap.append(poster);
    const refresh = () => { const n = new Image(); n.onload = () => { poster.src = n.src; poster.style.visibility = ''; }; n.src = still(big ? 1280 : 640); };
    refresh(); posterTimer = setInterval(() => { if (!el.classList.contains('ready')) refresh(); }, 1000);
    const P = await loadPlayer();
    if (!P) { showStill(); return; }
    const candidates = streamCandidates(cam, big);
    const stream = candidates[Math.min(attempt, candidates.length - 1)];
    player = document.createElement('camspire-video');
    player.className = 'player';
    player.background = false;
    player.mode = 'webrtc,mse,hls,mjpeg';
    wrap.append(player);
    player.src = wsUrl(stream);
    // Video läuft? (Element entsteht erst, wenn der Player im DOM hängt -> regelmäßig prüfen)
    const started = Date.now();
    timer = setInterval(() => {
      const v = player.video || player.querySelector('video');
      if (v && !v.dataset.styled) { v.style.cssText = 'width:100%;height:100%;object-fit:contain;background:transparent'; v.dataset.styled = 1; }
      if (v && v.readyState >= 2 && v.videoWidth > 0) {
        clearInterval(timer); clearInterval(posterTimer); el.classList.add('ready'); el.classList.remove('fallback');
        return;
      }
      if (Date.now() - started > 6000) {
        // Läuft nach 6 s nichts: nächste Variante probieren, zuletzt laufendes Standbild
        clearInterval(timer);
        if (attempt + 1 < candidates.length) showLive(attempt + 1);
        else showStill(true);
      }
    }, 250);
  };
  const showStill = (isFallback = false) => {
    clearTimeout(watchdog); clearInterval(posterTimer);
    wrap.innerHTML = `<img class="view" alt="">`;
    const img = $('img', wrap);
    const tick = () => { const n = new Image(); n.onload = () => (img.src = n.src); n.src = still(big ? 1280 : 640); };
    tick(); clearInterval(timer); timer = setInterval(tick, 1500);
    el.classList.toggle('fallback', isFallback);
  };
  const showAI = () => {
    clearTimeout(watchdog); clearInterval(posterTimer);
    wrap.innerHTML = `<img class="view" alt="">`;
    const img = $('img', wrap);
    const tick = () => { const n = new Image(); n.onload = () => (img.src = n.src); n.src = `/api/cameras/${cam.id}/latest.jpg?w=${big ? 1600 : 960}&t=${Date.now()}`; };
    tick(); clearInterval(timer); timer = setInterval(tick, big ? 400 : 700);
  };
  $('.ai', el).onclick = (e) => { e.stopPropagation(); mode = mode === 'live' ? 'ai' : 'live'; $('.ai', el).classList.toggle('on', mode === 'ai'); mode === 'ai' ? showAI() : showLive(); };
  const open = $('.open', el); if (open) open.onclick = (e) => { e.stopPropagation(); openCamera(cam); };
  el.updateCounts = (c = {}) => {
    const parts = ['person', 'vehicle', 'animal'].filter((g) => c[g] > 0).map((g) => `<span class="count c-${g}">${ICON[g]} ${c[g]}</span>`);
    $('.counts', el).innerHTML = parts.join('');
    el.classList.toggle('alert', (c.person || 0) > 0);
  };
  el.updateStatus = (st) => { const d = $('.dot', el); d.className = `dot ${st?.status || ''}`; };
  el.destroy = () => { clearInterval(timer); clearInterval(posterTimer); clearTimeout(watchdog); wrap.innerHTML = ''; };
  el.updateCounts(state.counts[cam.id] || cam.counts);
  showLive();
  return el;
}

// ---------------------------------------------------------------- Seiten
const pages = {};
let cleanup = null;

pages.live = async () => {
  main.innerHTML = `<div class="page-head"><h1>Live</h1><span class="sub">${state.cams.length} Kameras</span></div><div class="grid-live"></div>`;
  const grid = $('.grid-live');
  if (!state.cams.length) grid.innerHTML = `<div class="empty">${ICON.empty}Noch keine Kamera eingerichtet.<div style="margin-top:14px"><a class="btn primary" href="#/settings">Kamera hinzufügen</a></div></div>`;
  const tiles = state.cams.map((c) => { const t = camTile(c); grid.append(t); return t; });
  const fn = (m) => {
    if (m.type === 'counts') tiles.find((t) => t.dataset.cam === m.camera)?.updateCounts(m.counts);
    if (m.type === 'stats') tiles.forEach((t) => t.updateStatus(m.stats.cameras[t.dataset.cam]));
  };
  listeners.add(fn);
  return () => { listeners.delete(fn); tiles.forEach((t) => t.destroy()); };
};

async function openCamera(cam) {
  const card = openModal(`
    <div class="m-head"><h2>${esc(cam.name)}</h2><span class="spacer"></span>
      ${can('settings') ? '<button class="btn small" id="cam-edit">Einstellungen</button>' : ''}
      ${can('detect_toggle') ? `<span style="color:var(--muted)">Erkennung</span><button class="switch ${cam.stats?.enabled !== false ? 'on' : ''}" id="det"></button>` : ''}
      <button class="btn icon" data-close>${ICON.close}</button></div>
    <div class="m-body"><div id="tile"></div><h3 style="margin:0;font-size:15px">Letzte Ereignisse</h3><div class="grid-events" id="cam-ev"></div></div>`, true);
  const tile = camTile(cam, { big: true }); $('#tile', card).append(tile);
  if ($('#cam-edit', card)) $('#cam-edit', card).onclick = () => { state.editCam = cam.id; closeModal(); location.hash = '#/settings'; };
  if ($('#det', card)) $('#det', card).onclick = async (e) => {
    const on = !e.currentTarget.classList.contains('on');
    await api(`/api/cameras/${cam.id}/detect`, { method: 'POST', json: { enabled: on } });
    e.currentTarget.classList.toggle('on', on);
  };
  const evs = can('events') ? await api(`/api/events?camera=${cam.id}&limit=12`) : [];
  renderEventCards($('#cam-ev', card), evs, false);
  const fn = (m) => { if (m.type === 'counts' && m.camera === cam.id) tile.updateCounts(m.counts); };
  listeners.add(fn);
  card.onclose = () => { listeners.delete(fn); tile.destroy(); };
}

// ---------- Ereignisse
function eventCard(ev) {
  const el = document.createElement('div'); el.className = 'ev'; el.dataset.id = ev.id;
  const cam = state.cams.find((c) => c.id === ev.camera);
  const thumb = ev.thumb_url || `/api/cameras/${ev.camera}/latest.jpg?boxes=0&w=320`;
  el.innerHTML = `
    <div class="thumb"><img loading="lazy" src="${thumb}" alt="">
      <span class="tag count c-${ev.grp}">${ICON[ev.grp] || ''} ${esc(LABEL[ev.label] || ev.label)}</span>
      ${ev.active ? '<span class="live">LIVE</span>' : ''}
      ${ev.face_thumb_url ? `<img class="face" src="${ev.face_thumb_url}" alt="">` : ''}</div>
    <div class="body">
      <div class="title">${ev.sub_label ? `<span class="who">${esc(ev.sub_label)}</span>` : esc(LABEL[ev.label] || ev.label)}
        <span style="color:var(--muted);font-weight:400;font-size:12px;margin-left:auto">${Math.round((ev.top_score || 0) * 100)}%</span></div>
      <div class="meta"><span>${esc(cam?.name || ev.camera)}</span><span>${fmtTime(ev.start_ts)}</span><span>${fmtDur(ev.start_ts, ev.end_ts)}</span>
        ${(ev.zones || []).map((z) => `<span class="pill">${esc(z)}</span>`).join('')}</div>
    </div>`;
  el.onclick = () => openEvent(ev.id);
  return el;
}

function renderEventCards(container, evs, withDays = true, append = false) {
  if (!append) container.innerHTML = '';
  if (!evs.length && !append) { container.innerHTML = `<div class="empty">${ICON.empty}Noch keine Ereignisse</div>`; return; }
  let last = append ? container.dataset.lastDay : null;
  for (const ev of evs) {
    const d = fmtDay(ev.start_ts);
    if (withDays && d !== last) { const h = document.createElement('div'); h.className = 'day'; h.textContent = d; container.append(h); last = d; }
    container.append(eventCard(ev));
  }
  container.dataset.lastDay = last || '';
}

pages.events = async () => {
  const f = state.eventsFilter;
  main.innerHTML = `
    <div class="page-head"><h1>Ereignisse</h1><span class="spacer"></span></div>
    <div class="filters">
      <select id="f-cam"><option value="">Alle Kameras</option>${state.cams.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join('')}</select>
      <div class="chips" id="f-grp">${['', 'person', 'vehicle', 'animal'].map((g) => `<span class="chip ${f.grp === g ? 'on' : ''}" data-g="${g}">${g ? ICON[g] + GROUP[g] : 'Alle'}</span>`).join('')}</div>
      <select id="f-person"><option value="">Alle Gesichter</option><option value="__known__">Nur bekannte</option><option value="__unknown__">Nur unbekannte</option>
        ${state.persons.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join('')}</select>
    </div>
    <div class="grid-events" id="evs"></div>
    <div style="text-align:center;margin-top:20px"><button class="btn" id="more" hidden>Ältere laden</button></div>`;
  $('#f-cam').value = f.camera; $('#f-person').value = f.person;
  const grid = $('#evs'); let oldest = null;
  const q = () => new URLSearchParams(Object.entries({ camera: f.camera, grp: f.grp, person: f.person, limit: 60 }).filter(([, v]) => v)).toString();
  const load = async (more = false) => {
    const evs = await api(`/api/events?${q()}${more && oldest ? `&before=${oldest}` : ''}`);
    renderEventCards(grid, evs, true, more);
    if (evs.length) oldest = evs[evs.length - 1].start_ts;
    $('#more').hidden = evs.length < 60;
  };
  $('#f-cam').onchange = (e) => { f.camera = e.target.value; load(); };
  $('#f-person').onchange = (e) => { f.person = e.target.value; load(); };
  $$('#f-grp .chip').forEach((c) => (c.onclick = () => { f.grp = c.dataset.g; $$('#f-grp .chip').forEach((x) => x.classList.toggle('on', x === c)); load(); }));
  $('#more').onclick = () => load(true);
  await load();
  const matches = (ev) => (!f.camera || ev.camera === f.camera) && (!f.grp || ev.grp === f.grp) && (!f.person || f.person.startsWith('__') || ev.sub_label === f.person);
  const fn = (m) => {
    if (m.type !== 'event') return;
    const ev = m.event;
    const existing = grid.querySelector(`.ev[data-id="${ev.id}"]`);
    if (m.action === 'new' && matches(ev) && !existing) {
      $('.empty', grid)?.remove();
      const card = eventCard({ ...ev, active: true });
      const firstDay = grid.querySelector('.day');
      if (firstDay && firstDay.textContent === 'Heute') firstDay.after(card);
      else { const h = document.createElement('div'); h.className = 'day'; h.textContent = 'Heute'; grid.prepend(h, card); }
    } else if (existing) {
      const fresh = eventCard({ ...ev, active: m.action !== 'end', thumb_url: ev.thumb ? `/media/${ev.thumb}` : null, face_thumb_url: ev.face_thumb ? `/media/${ev.face_thumb}` : null });
      existing.replaceWith(fresh);
    }
  };
  listeners.add(fn);
  return () => listeners.delete(fn);
};

async function openEvent(id) {
  const ev = await api(`/api/events/${id}`);
  const cam = state.cams.find((c) => c.id === ev.camera);
  const media = ev.clip
    ? `<video class="m-media" id="evvideo" ${ev.clip.hls ? '' : `src="${ev.clip.url}#t=${ev.clip.offset.toFixed(1)}"`} controls autoplay muted playsinline poster="${ev.snapshot_url || ''}"></video>`
    : ev.snapshot_url ? `<img class="m-media" src="${ev.snapshot_url}">`
    : `<img class="m-media" id="livefeed" src="/api/cameras/${ev.camera}/latest.jpg?w=1280">`;
  const faceBlock = ev.grp === 'person' && state.facesEnabled && can('events_manage') ? `
    <div class="card" style="padding:14px">
      <h3>Gesicht</h3>
      ${ev.face_thumb_url ? `<img src="${ev.face_thumb_url}" style="width:96px;border-radius:14px;display:block;margin-bottom:10px">` : '<div style="color:var(--muted);margin-bottom:10px">Kein Gesicht erfasst</div>'}
      ${ev.face_thumb_url ? `
      <div style="display:flex;flex-direction:column;gap:8px">
        <label style="color:var(--muted);font-size:12px">${ev.sub_label ? 'Falsch erkannt? Korrigieren:' : 'Wer ist das?'}</label>
        <input type="text" id="who" list="plist" placeholder="Name eingeben" value="${esc(ev.sub_label || '')}">
        <datalist id="plist">${state.persons.map((p) => `<option value="${esc(p.name)}">`).join('')}</datalist>
        <div style="display:flex;gap:8px"><button class="btn primary small" id="who-save">Speichern & lernen</button>
        ${ev.sub_label ? '<button class="btn small" id="who-none">Niemand davon</button>' : ''}</div>
      </div>` : ''}
    </div>` : '';
  const card = openModal(`
    <div class="m-head"><span class="count c-${ev.grp}">${ICON[ev.grp] || ''} ${esc(LABEL[ev.label] || ev.label)}</span>
      <h2>${ev.sub_label ? esc(ev.sub_label) : ''}</h2><span class="spacer"></span>
      ${can('events_manage') ? `<button class="btn icon danger" id="del" title="Löschen">${ICON.trash}</button>` : ''}<button class="btn icon" data-close>${ICON.close}</button></div>
    <div class="m-body"><div class="m-cols">
      <div>${media}</div>
      <div style="display:flex;flex-direction:column;gap:14px">
        <div class="card" style="padding:14px"><dl class="kv">
          <dt>Kamera</dt><dd>${esc(cam?.name || ev.camera)}</dd>
          <dt>Datum</dt><dd>${new Date(ev.start_ts * 1000).toLocaleDateString('de-DE')}</dd>
          <dt>Beginn</dt><dd>${fmtTime(ev.start_ts)}</dd>
          <dt>Dauer</dt><dd>${fmtDur(ev.start_ts, ev.end_ts)}</dd>
          <dt>Sicherheit</dt><dd>${Math.round((ev.top_score || 0) * 100)}%</dd>
          ${ev.sub_label ? `<dt>Gesicht</dt><dd class="who">${esc(ev.sub_label)}${ev.sub_score ? ` (${Math.round(ev.sub_score * 100)}%)` : ''}</dd>` : ''}
          ${ev.zones?.length ? `<dt>Zonen</dt><dd>${ev.zones.map(esc).join(', ')}</dd>` : ''}
        </dl></div>
        ${faceBlock}
        ${ev.snapshot_url && can('download') ? `<a class="btn small" href="${ev.snapshot_url}" download>Snapshot herunterladen</a>` : ''}
        ${ev.clip && can('download') ? `<a class="btn small" href="${ev.clip.url}" download>Clip herunterladen (MP4)</a>` : ''}
        ${can('history') ? `<a class="btn small" href="#/history?cam=${ev.camera}&t=${(ev.start_ts - 5).toFixed(0)}" id="ev-hist">Im Verlauf ansehen</a>` : ''}
      </div></div></div>`);
  const evv = $('#evvideo', card);
  if (evv && ev.clip?.hls) playHls(evv, ev.clip.hls, ev.clip.offset);
  const eh = $('#ev-hist', card); if (eh) eh.onclick = () => closeModal();
  const lf = $('#livefeed', card);
  if (lf) { const t = setInterval(() => { lf.src = `/api/cameras/${ev.camera}/latest.jpg?w=1280&t=${Date.now()}`; }, 500); card.onclose = () => clearInterval(t); }
  if ($('#del', card)) $('#del', card).onclick = async () => {
    if (!confirm('Ereignis und zugehörige Aufnahme löschen?')) return;
    await api(`/api/events/${id}`, { method: 'DELETE' }); closeModal(); route();
  };
  const save = $('#who-save', card);
  if (save) save.onclick = async () => {
    const name = $('#who', card).value.trim(); if (!name) return;
    await api(`/api/events/${id}/face`, { method: 'POST', json: { name } });
    toast(`${ICON.sparkle}<div>Gelernt: <b class="who">${esc(name)}</b></div>`); await loadPersons(); closeModal(); route();
  };
  const none = $('#who-none', card);
  if (none) none.onclick = async () => { await api(`/api/events/${id}/face`, { method: 'POST', json: {} }); closeModal(); route(); };
}

// ---------- Gesichter
async function loadPersons() { try { state.persons = await api('/api/faces/persons'); } catch { state.persons = []; } }

pages.faces = async () => {
  if (!state.facesEnabled) { main.innerHTML = `<div class="page-head"><h1>Gesichter</h1></div><div class="empty">${ICON.empty}Gesichtserkennung ist in der Konfiguration deaktiviert.</div>`; return; }
  const tab = state.facesTab || 'unknown';
  await loadPersons();
  const clusters = await api('/api/faces/clusters');
  updateUnknownBadge(clusters.length);
  main.innerHTML = `
    <div class="page-head"><h1>Gesichter</h1>
      <div class="tabs"><button data-t="unknown" class="${tab === 'unknown' ? 'on' : ''}">Unbekannt (${clusters.length})</button>
        <button data-t="persons" class="${tab === 'persons' ? 'on' : ''}">Personen (${state.persons.length})</button></div>
      <span class="spacer"></span>${can('faces') ? `<button class="btn primary" id="add">${ICON.plus} Person anlegen</button>` : ''}</div>
    <div id="fc"></div>`;
  $$('.tabs button').forEach((b) => (b.onclick = () => { state.facesTab = b.dataset.t; route(); }));
  if ($('#add')) $('#add').onclick = async () => {
    const name = prompt('Name der Person'); if (!name) return;
    const r = await api('/api/faces/persons', { method: 'POST', json: { name } }); await loadPersons();
    openPerson(state.persons.find((p) => p.id === r.id));
  };
  const fc = $('#fc');
  if (tab === 'persons') {
    if (!state.persons.length) { fc.innerHTML = `<div class="empty">${ICON.empty}Noch keine Personen. Benenne unbekannte Gesichter oder lege eine Person an und lade Fotos hoch.</div>`; return; }
    fc.innerHTML = `<div class="grid-people">${state.persons.map((p) => `
      <div class="person" data-id="${p.id}">
        ${p.cover_url ? `<img class="avatar" src="${p.cover_url}">` : `<div class="avatar" style="display:grid;place-items:center">${ICON.person}</div>`}
        <div class="n">${esc(p.name)}</div>
        <div class="m">${p.samples} Referenzen${p.learned ? ` · ${p.learned} gelernt` : ''}</div>
        <div class="m">${p.last_seen ? `zuletzt ${fmtAgo(p.last_seen)}` : 'noch nicht gesehen'}</div>
      </div>`).join('')}</div>`;
    $$('.person', fc).forEach((el) => (el.onclick = () => openPerson(state.persons.find((p) => p.id === +el.dataset.id))));
  } else {
    if (!clusters.length) { fc.innerHTML = `<div class="empty">${ICON.empty}Keine unbekannten Gesichter – alles zugeordnet.</div>`; return; }
    fc.innerHTML = `<p style="color:var(--muted);margin-top:0">Ähnliche Gesichter werden automatisch gruppiert. Einmal benennen – CamSpire lernt daraus und erkennt die Person künftig selbst.</p>
      <datalist id="plist">${state.persons.map((p) => `<option value="${esc(p.name)}">`).join('')}</datalist>
      <div class="clusters">${clusters.map((c) => `
      <div class="cluster" data-id="${c.id}">
        <div class="strip">${c.samples.map((s) => `<img src="${s.url}" title="${s.camera || ''}">`).join('')}</div>
        <div style="color:var(--muted);font-size:12px">${c.count}× gesehen · zuletzt ${fmtAgo(c.last_seen)}</div>
        ${c.suggestion ? `<div class="suggest">${ICON.sparkle}<span>Vielleicht <b>${esc(c.suggestion.name)}</b>?</span><span class="spacer"></span><button class="btn small primary" data-yes="${c.suggestion.person_id}">Ja</button></div>` : ''}
        <div class="actions"><input type="text" list="plist" placeholder="Wer ist das?"><button class="btn primary" data-save>Zuordnen</button>
          <button class="btn icon danger" data-del title="Verwerfen">${ICON.trash}</button></div>
      </div>`).join('')}</div>`;
    $$('.cluster', fc).forEach((el) => {
      const id = el.dataset.id;
      const done = () => { el.style.opacity = .3; setTimeout(() => el.remove(), 250); };
      $('[data-save]', el).onclick = async () => {
        const name = $('input', el).value.trim(); if (!name) return $('input', el).focus();
        const r = await api(`/api/faces/clusters/${id}/assign`, { method: 'POST', json: { name } });
        toast(`${ICON.sparkle}<div><b>${r.assigned}</b> Bilder als <b class="who">${esc(name)}</b> gelernt</div>`); done(); loadPersons();
      };
      $('input', el).onkeydown = (e) => { if (e.key === 'Enter') $('[data-save]', el).click(); };
      const yes = $('[data-yes]', el);
      if (yes) yes.onclick = async () => { await api(`/api/faces/clusters/${id}/assign`, { method: 'POST', json: { person_id: +yes.dataset.yes } }); done(); };
      $('[data-del]', el).onclick = async () => { await api(`/api/faces/clusters/${id}`, { method: 'DELETE' }); done(); };
    });
  }
};

async function openPerson(p) {
  if (!p) return;
  const card = openModal(`
    <div class="m-head"><h2>${esc(p.name)}</h2><span class="spacer"></span>
      <button class="btn small" id="ren">Umbenennen</button><button class="btn icon danger" id="pdel">${ICON.trash}</button>
      <button class="btn icon" data-close>${ICON.close}</button></div>
    <div class="m-body">
      <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
        <label class="btn primary">${ICON.upload} Fotos hochladen<input type="file" accept="image/*" multiple hidden id="up"></label>
        <span style="color:var(--muted);font-size:13px">Mehrere Fotos (frontal, seitlich, mit/ohne Brille) verbessern die Erkennung. Gelernte Bilder (Auto) fügt CamSpire selbst hinzu.</span>
      </div>
      <div class="samples" id="smp"></div>
    </div>`);
  const load = async () => {
    const s = await api(`/api/faces/persons/${p.id}/samples`);
    const SRC = { manual: 'Foto', auto: 'Auto', confirmed: 'bestätigt' };
    $('#smp', card).innerHTML = s.length ? s.map((x) => `<div class="sample"><img src="${x.url}" title="Qualität ${Math.round(x.quality * 100)}%"><span class="src">${SRC[x.source] || x.source}</span><button data-id="${x.id}" title="Entfernen">${ICON.close}</button></div>`).join('')
      : `<div class="empty" style="grid-column:1/-1">Noch keine Referenzbilder</div>`;
    $$('.sample button', card).forEach((b) => (b.onclick = async () => { await api(`/api/faces/samples/${b.dataset.id}`, { method: 'DELETE' }); load(); }));
  };
  $('#up', card).onchange = async (e) => {
    const fd = new FormData(); [...e.target.files].forEach((f) => fd.append('files', f));
    const r = await api(`/api/faces/persons/${p.id}/upload`, { method: 'POST', body: fd });
    toast(`<div>${r.added} von ${r.total} Fotos übernommen${r.added < r.total ? ' (kein Gesicht gefunden)' : ''}</div>`); load();
  };
  $('#ren', card).onclick = async () => { const n = prompt('Neuer Name', p.name); if (!n) return; await api(`/api/faces/persons/${p.id}`, { method: 'PATCH', json: { name: n } }); closeModal(); route(); };
  $('#pdel', card).onclick = async () => { if (!confirm(`${p.name} und alle Referenzbilder löschen?`)) return; await api(`/api/faces/persons/${p.id}`, { method: 'DELETE' }); closeModal(); route(); };
  card.onclose = () => { if (state.page === 'faces') route(); };
  load();
}

async function updateUnknownBadge(n) {
  if (n === undefined) { if (!state.facesEnabled || !(can('events') || can('faces'))) return; try { n = (await api('/api/faces/clusters')).length; } catch { return; } }
  const b = $('#nav-unknown'); b.hidden = !n; b.textContent = n;
}

// ---------- System
pages.system = async () => {
  main.innerHTML = `<div class="page-head"><h1>System</h1><span class="spacer"></span><button class="btn" id="clean">Jetzt aufräumen</button></div><div class="grid-sys" id="sys"></div>`;
  const summary = await api('/api/events/summary?days=7').catch(() => null);
  const render = (s) => {
    if (!s) return;
    const g = (s.gpu || [])[0];
    const st = s.storage || {};
    const pct = (v) => st.limit ? Math.min(100, (v / st.limit) * 100) : 0;
    const hours = Array.from({ length: 24 }, (_, h) => (summary?.by_hour || []).find((x) => x.h === h)?.n || 0);
    const hmax = Math.max(1, ...hours);
    $('#sys').innerHTML = `
      <div class="card"><h3>GPU</h3>${g ? `
        <div class="big">${g.util ?? '–'}<span style="font-size:16px;color:var(--muted)"> % Last</span></div>
        <dl class="kv" style="margin-top:10px"><dt>Modell</dt><dd>${esc(g.name)}</dd>
        <dt>Video-Decoder</dt><dd>${g.decoder ?? '–'} %</dd>
        <dt>VRAM</dt><dd>${g.mem_used} / ${g.mem_total} MiB</dd><dt>Temperatur</dt><dd>${g.temp ?? '–'} °C</dd>
        ${g.power != null ? `<dt>Leistung</dt><dd>${g.power} W</dd>` : ''}</dl>` : '<div style="color:var(--muted)">Keine NVIDIA-GPU sichtbar (nvidia-smi)</div>'}</div>
      <div class="card"><h3>Erkennung</h3>
        <div class="big">${s.detector.infer_ms ?? '–'}<span style="font-size:16px;color:var(--muted)"> ms / Bild</span></div>
        <dl class="kv" style="margin-top:10px"><dt>Backend</dt><dd><span class="pill ${String(s.detector.provider).includes('CPU') ? 'bad' : 'ok'}">${esc((s.detector.provider || 'aus').replace('ExecutionProvider', ''))}</span></dd>
        <dt>Eingabe</dt><dd>${s.detector.input || '–'} px</dd><dt>Inferenzen</dt><dd>${(s.detector.calls || 0).toLocaleString('de-DE')}</dd>
        <dt>Aktive Ereignisse</dt><dd>${s.active_events}</dd></dl></div>
      <div class="card"><h3>Gesichtserkennung</h3>
        <dl class="kv"><dt>Status</dt><dd>${s.faces.enabled ? '<span class="pill ok">aktiv</span>' : '<span class="pill">aus</span>'}</dd>
        ${s.faces.enabled ? `<dt>Backend</dt><dd>${esc((s.faces.provider || '').replace('ExecutionProvider', ''))}</dd>
        <dt>Bekannte Personen</dt><dd>${s.faces.known}</dd><dt>Analysiert</dt><dd>${s.faces.processed.toLocaleString('de-DE')}</dd>
        <dt>Übersprungen (Last)</dt><dd>${s.faces.dropped}</dd>` : ''}</dl></div>
      <div class="card"><h3>Speicher</h3>
        <div class="big">${fmtBytes(st.total)}<span style="font-size:16px;color:var(--muted)"> von ${fmtBytes(st.limit)}</span></div>
        <div class="bar"><i style="width:${pct(st.events)}%;background:var(--accent)"></i><i style="width:${pct(st.continuous)}%;background:var(--accent-2)"></i><i style="width:${pct(st.snapshots)}%;background:var(--person)"></i><i style="width:${pct(st.faces)}%;background:var(--animal)"></i></div>
        <div class="legend"><span style="--c:var(--accent)">Ereignisse ${fmtBytes(st.events)}</span><span style="--c:var(--accent-2)">Dauer ${fmtBytes(st.continuous)}</span>
          <span style="--c:var(--person)">Snapshots ${fmtBytes(st.snapshots)}</span><span style="--c:var(--animal)">Gesichter ${fmtBytes(st.faces)}</span></div>
        <dl class="kv" style="margin-top:12px">
          <dt>Speicherort</dt><dd>${!st.storage || st.storage.type === 'local' ? 'lokal' : st.storage.ok ? `<span class="pill ok">NAS</span> ${esc(st.storage.target)}` : `<span class="pill bad">NAS getrennt</span>`}</dd>
          ${st.on_nas ? `<dt>Davon auf dem NAS</dt><dd>${fmtBytes(st.on_nas)}</dd>` : ''}
          ${st.storage?.usage ? `<dt>Frei auf dem NAS</dt><dd>${fmtBytes(st.storage.usage.free)}</dd>` : ''}
          <dt>Frei lokal</dt><dd>${fmtBytes(st.disk_free)}</dd>
          ${st.oldest ? `<dt>Älteste Aufnahme</dt><dd>${new Date(st.oldest * 1000).toLocaleDateString('de-DE')}</dd>` : ''}</dl></div>
      <div class="card" style="grid-column:1/-1"><h3>Kameras</h3><div style="overflow-x:auto"><table>
        <tr><th>Kamera</th><th>Status</th><th>Auflösung</th><th>Codec · Keyframe</th><th>Dekodierung</th><th>Bilder/s</th><th>KI/s</th><th>Bewegung</th><th>Aufnahme</th></tr>
        ${Object.entries(s.cameras).map(([id, c]) => { const r = s.recording[id]; const cam = state.cams.find((x) => x.id === id);
          return `<tr><td>${esc(cam?.name || id)}</td><td><span class="pill ${c.status === 'online' ? 'ok' : 'bad'}">${c.status}</span></td>
          <td>${c.resolution.join('×')}</td><td>${['main', 'sub'].map((k) => s.codecs?.[id]?.[k]).filter(Boolean).map((x) => `${esc(String(x.codec).toUpperCase())}${x.gop ? ` <span class="${x.gop > 2.5 ? 'bad' : 'muted'}" title="Keyframe-Abstand – über 2,5 s verzögert den Bildaufbau">· ${x.gop}s</span>` : ''}`).join(' / ') || '–'}</td><td>${c.hwaccel ? 'GPU (NVDEC)' : 'CPU'}</td><td>${c.decode_fps}</td><td>${c.enabled ? c.detect_fps : 'aus'}</td>
          <td>${c.motion ? '●' : '–'}</td><td>${r ? (r.recording ? `<span class="pill bad">● ${r.mode === 'continuous' ? 'Dauer' : 'REC'}</span>` : (Date.now() / 1000 - r.last_segment < 15 ? 'bereit' : '<span class="pill bad">kein Stream</span>')) : 'aus'}</td></tr>`; }).join('')}
      </table></div></div>
      <div class="card"><h3>Aktivität (7 Tage, nach Uhrzeit)</h3><div class="hours">${hours.map((n, h) => `<i title="${h}:00 – ${n}" style="height:${(n / hmax) * 100}%"></i>`).join('')}</div>
        <div style="display:flex;justify-content:space-between;color:var(--muted);font-size:11px;margin-top:4px"><span>0</span><span>6</span><span>12</span><span>18</span><span>23</span></div>
        ${(summary?.people || []).length ? `<dl class="kv" style="margin-top:14px">${summary.people.slice(0, 6).map((p) => `<dt class="who">${esc(p.name)}</dt><dd>${p.n}×</dd>`).join('')}</dl>` : ''}</div>
      <div class="card"><h3>Dienste</h3><dl class="kv">
        <dt>Version</dt><dd>${esc(s.version)}</dd><dt>Laufzeit</dt><dd>${fmtUp(s.uptime)}</dd>
        <dt>go2rtc</dt><dd>${s.go2rtc ? '<span class="pill ok">läuft</span>' : '<span class="pill bad">aus</span>'}</dd>
        <dt>MQTT / Home Assistant</dt><dd>${!s.mqtt.enabled ? '<span class="pill">aus</span>' : s.mqtt.connected ? '<span class="pill ok">verbunden</span>' : '<span class="pill bad">getrennt</span>'}</dd>
        <dt>API-Doku</dt><dd><a href="/api/docs" target="_blank" style="color:var(--accent)">/api/docs</a></dd></dl></div>`;
  };
  render(await api('/api/stats'));
  $('#clean').onclick = async () => { $('#clean').disabled = true; await api('/api/storage/cleanup', { method: 'POST' }); render(await api('/api/stats')); $('#clean').disabled = false; toast('Aufgeräumt'); };
  const t = setInterval(async () => render(await api('/api/stats').catch(() => null)), 3000);
  return () => clearInterval(t);
};

// ---------------------------------------------------------------- Modal & Router
function openModal(html, wide = false) {
  const m = $('#modal'), c = $('#modal-card');
  if (c.onclose) c.onclose();
  c.onclose = null; c.className = `modal-card${wide ? ' wide' : ''}`; c.innerHTML = html; m.hidden = false;
  $$('[data-close]', c).forEach((b) => (b.onclick = closeModal));
  return c;
}
function closeModal() {
  const m = $('#modal'), c = $('#modal-card');
  $$('video', c).forEach((v) => { v.pause(); if (v._hls) v._hls.destroy(); });
  if (c.onclose) { const f = c.onclose; c.onclose = null; f(); }
  c.innerHTML = ''; m.hidden = true;
}
$('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('#modal').hidden) closeModal(); });

async function route() {
  let page = (location.hash.replace(/^#\//, '') || 'live').split('?')[0];
  if (state.me && !pageAllowed(page)) page = ['live', 'events', 'history'].find(pageAllowed) || 'settings';
  const fn = pages[page] || pages.live;
  if (cleanup) { try { cleanup(); } catch {} cleanup = null; }
  if (!$('#modal').hidden) closeModal();
  state.page = page;
  $$('.nav a').forEach((a) => a.classList.toggle('active', a.dataset.page === page));
  try { cleanup = (await fn()) || null; } catch (e) { main.innerHTML = `<div class="empty">Fehler: ${esc(e.message)}</div>`; }
  const ev = new URLSearchParams(location.hash.split('?')[1] || '').get('ev');
  if (page === 'events' && ev) openEvent(ev).catch(() => toast('Ereignis nicht mehr vorhanden'));
}
window.addEventListener('hashchange', route);

function helpers() { return { api, esc, toast, openModal, closeModal, ICON, $, $$, main, reloadConfig, camTile, state, can }; }
pages.history = () => {
  const q = new URLSearchParams(location.hash.split('?')[1] || '');
  return historyPage(helpers(), Object.fromEntries(q));
};
pages.settings = () => { const edit = state.editCam; state.editCam = null; return settingsPage(helpers(), edit); };

async function reloadConfig() {
  const cfg = await api('/api/config');
  state.cams = cfg.cameras;
  state.me = cfg.me; applyNav();
  state.facesEnabled = !!cfg.faces_active;
  cfg.cameras.forEach((c) => (state.counts[c.id] ??= c.counts));
  return cfg;
}

const can = (p) => state.me && (state.me.is_admin || state.me.perms.includes(p));
const PAGE_PERM = { live: ['live'], history: ['history'], events: ['events'], faces: ['events', 'faces'], system: ['system'], settings: [] };
const pageAllowed = (pg) => (PAGE_PERM[pg] || []).length === 0 || PAGE_PERM[pg].some(can);

function applyNav() {
  $$('.nav a[data-page]').forEach((a) => { a.hidden = !pageAllowed(a.dataset.page); });
  const st = $('.nav a[data-page="settings"] span');
  if (st) { st.textContent = can('settings') ? 'Einstellungen' : 'Konto'; st.dataset.short = can('settings') ? 'Optionen' : 'Konto'; }
  const me = state.me;
  let chip = $('#me-chip');
  if (!chip) { chip = document.createElement('div'); chip.id = 'me-chip'; chip.className = 'me-chip'; $('.nav').insertBefore(chip, $('#conn')); }
  chip.innerHTML = `<a href="#/settings" class="me-link" title="Mein Konto"><i>${esc((me.display_name || me.username).slice(0, 1).toUpperCase())}</i><span>${esc(me.display_name || me.username)}</span></a>
    <button class="btn icon small" id="me-out" title="Abmelden"><svg viewBox="0 0 24 24"><path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 17l5-5-5-5M15 12H3"/></svg></button>`;
  $('#me-out').onclick = logout;
}

(async function boot() {
  registerSW();
  initAccount(helpers());
  const status = await (await fetch('/api/auth/status')).json();
  if (status.setup_required || !status.user) { renderAuthScreen(status); return; }
  const cfg = await api('/api/config');
  state.me = cfg.me;
  applyNav();
  if (!cfg.cameras.length && !location.hash && can('settings')) location.hash = '#/settings';
  else if (!location.hash || !pageAllowed(location.hash.replace(/^#\//, '').split('?')[0])) {
    const first = ['live', 'events', 'history'].find(pageAllowed) || 'settings'; location.hash = `#/${first}`;
  }
  state.cams = cfg.cameras;
  state.facesEnabled = !!cfg.faces_active;
  cfg.cameras.forEach((c) => (state.counts[c.id] = c.counts));
  if (can('events') || can('faces')) await loadPersons();
  connectWS();
  route();
  updateUnknownBadge();
  setInterval(updateUnknownBadge, 60000);
})();
