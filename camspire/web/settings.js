import { accountTab, initAccount, usersTab } from './account.js';
// CamSpire NVR – Einstellungen (Kameras, Erkennung, Gesichter, Aufnahme, Home Assistant, Zugang)
let H; // Helfer aus app.js: { api, esc, toast, openModal, closeModal, ICON, $, $$, main, reloadConfig }

const LABEL_DE = { person: 'Person', bicycle: 'Fahrrad', car: 'Auto', motorcycle: 'Motorrad', bus: 'Bus', truck: 'LKW',
  boat: 'Boot', train: 'Zug', airplane: 'Flugzeug', bird: 'Vogel', cat: 'Katze', dog: 'Hund', horse: 'Pferd',
  sheep: 'Schaf', cow: 'Kuh', elephant: 'Elefant', bear: 'Bär', zebra: 'Zebra', giraffe: 'Giraffe' };
const GROUP_DE = { person: 'Personen', vehicle: 'Fahrzeuge', animal: 'Tiere' };
const ZONE_COLORS = ['#ff78c8', '#5eead4', '#ffc850', '#3caaff', '#a78bfa', '#78e678'];

// Hersteller-Vorlagen für RTSP-Adressen
const TEMPLATES = {
  hikvision: { name: 'Hikvision / Annke / HiLook', main: 'rtsp://{u}:{p}@{ip}:554/Streaming/Channels/101', sub: 'rtsp://{u}:{p}@{ip}:554/Streaming/Channels/102' },
  dahua: { name: 'Dahua / Amcrest / Lorex', main: 'rtsp://{u}:{p}@{ip}:554/cam/realmonitor?channel=1&subtype=0', sub: 'rtsp://{u}:{p}@{ip}:554/cam/realmonitor?channel=1&subtype=1' },
  reolink: { name: 'Reolink (H.264)', main: 'rtsp://{u}:{p}@{ip}:554/h264Preview_01_main', sub: 'rtsp://{u}:{p}@{ip}:554/h264Preview_01_sub' },
  reolink265: { name: 'Reolink (H.265, 4K)', main: 'rtsp://{u}:{p}@{ip}:554/h265Preview_01_main', sub: 'rtsp://{u}:{p}@{ip}:554/h264Preview_01_sub' },
  tapo: { name: 'TP-Link Tapo / VIGI', main: 'rtsp://{u}:{p}@{ip}:554/stream1', sub: 'rtsp://{u}:{p}@{ip}:554/stream2' },
  foscam: { name: 'Foscam', main: 'rtsp://{u}:{p}@{ip}:554/videoMain', sub: 'rtsp://{u}:{p}@{ip}:554/videoSub' },
  axis: { name: 'Axis', main: 'rtsp://{u}:{p}@{ip}/axis-media/media.amp?resolution=1920x1080', sub: 'rtsp://{u}:{p}@{ip}/axis-media/media.amp?resolution=640x360' },
  uniview: { name: 'Uniview', main: 'rtsp://{u}:{p}@{ip}:554/media/video1', sub: 'rtsp://{u}:{p}@{ip}:554/media/video2' },
  onvif: { name: 'ONVIF (automatisch)', main: 'onvif://{u}:{p}@{ip}:80', sub: '' },
};

const S = { cfg: null, orig: '', meta: null, tab: 'cameras', stats: null };
const clone = (o) => JSON.parse(JSON.stringify(o));
const getP = (o, p) => p.split('.').reduce((a, k) => (a == null ? a : a[k]), o);
const setP = (o, p, v) => { const ks = p.split('.'); const last = ks.pop(); const t = ks.reduce((a, k) => (a[k] ??= {}), o); t[last] = v; };
const slug = (s) => String(s || '').toLowerCase().replace(/ä/g, 'ae').replace(/ö/g, 'oe').replace(/ü/g, 'ue').replace(/ß/g, 'ss')
  .replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 40);
const dirty = () => !!S.cfg && JSON.stringify(S.cfg) !== S.orig;
const maskUrl = (u) => String(u || '').replace(/\/\/([^:@/]+):([^@/]+)@/, '//$1:•••@');

// ---------------------------------------------------------------- Formular-Bausteine
const row = (label, control, hint = '') => `<div class="f-row"><div class="f-label">${label}${hint ? `<small>${hint}</small>` : ''}</div><div class="f-ctl">${control}</div></div>`;
const toggle = (path, obj = 'cfg') => `<button type="button" class="switch" data-bind="${path}" data-kind="bool" data-obj="${obj}"></button>`;
const text = (path, ph = '', type = 'text', obj = 'cfg') => `<input type="${type}" data-bind="${path}" data-kind="str" data-obj="${obj}" placeholder="${H.esc(ph)}" autocomplete="off" spellcheck="false">`;
const num = (path, min, max, step = 1, obj = 'cfg') => `<input type="number" data-bind="${path}" data-kind="num" data-obj="${obj}" min="${min}" max="${max}" step="${step}">`;
const range = (path, min, max, step, fmt = 'v', obj = 'cfg') => `<div class="range"><input type="range" data-bind="${path}" data-kind="num" data-obj="${obj}" min="${min}" max="${max}" step="${step}"><output data-fmt="${fmt}"></output></div>`;
const select = (path, opts, obj = 'cfg') => `<select data-bind="${path}" data-kind="str" data-obj="${obj}">${opts.map(([v, l]) => `<option value="${H.esc(v)}">${H.esc(l)}</option>`).join('')}</select>`;
const seg = (path, opts, obj = 'cfg') => `<div class="seg" data-bind="${path}" data-kind="seg" data-obj="${obj}">${opts.map(([v, l]) => `<button type="button" data-v="${H.esc(v)}">${H.esc(l)}</button>`).join('')}</div>`;
const fmtOut = (fmt, v) => fmt === 'pct1' ? `${(v * 100).toFixed(2).replace('.', ',')} %` : fmt === 'pct' ? `${Math.round(v * 100)} %` : fmt === 's' ? `${v} s` : fmt === 'fps' ? `${v} fps` : fmt === 'px' ? `${v} px` : String(v);

// Bindet alle [data-bind]-Elemente in root an ein Objekt (objs[name])
function bind(root, objs, onChange) {
  H.$$('[data-bind]', root).forEach((el) => {
    const o = objs[el.dataset.obj || 'cfg']; const p = el.dataset.bind; const k = el.dataset.kind;
    const out = el.parentElement.querySelector('output');
    const show = () => { if (out) out.textContent = fmtOut(out.dataset.fmt, el.value); };
    const v = getP(o, p);
    if (k === 'bool') {
      el.classList.toggle('on', !!v);
      el.onclick = () => { const nv = !getP(o, p); setP(o, p, nv); el.classList.toggle('on', nv); onChange(p, nv); };
    } else if (k === 'seg') {
      const sync = () => H.$$('button', el).forEach((b) => b.classList.toggle('on', b.dataset.v === String(getP(o, p))));
      sync();
      H.$$('button', el).forEach((b) => (b.onclick = () => { setP(o, p, b.dataset.v); sync(); onChange(p, b.dataset.v); }));
    } else {
      el.value = v ?? '';
      show();
      el.oninput = () => {
        let nv = el.value;
        if (k === 'num') nv = nv === '' ? null : Number(nv);
        setP(o, p, nv); show(); onChange(p, nv);
      };
    }
  });
}

function labelChips(selected, onToggle, labels) {
  const groups = { person: [], vehicle: [], animal: [] };
  labels.forEach((l) => groups[S.meta.groups[l]]?.push(l));
  return Object.entries(groups).map(([g, ls]) => `<div class="chipgroup"><span>${GROUP_DE[g]}</span><div class="chips">${ls.map((l) =>
    `<span class="chip ${selected.includes(l) ? 'on' : ''}" data-l="${l}">${H.esc(LABEL_DE[l] || l)}</span>`).join('')}</div></div>`).join('');
}
function bindChips(root, list, onChange) {
  H.$$('.chip[data-l]', root).forEach((c) => (c.onclick = () => {
    const l = c.dataset.l; const i = list.indexOf(l);
    if (i >= 0) list.splice(i, 1); else list.push(l);
    c.classList.toggle('on', i < 0); onChange();
  }));
}

// ---------------------------------------------------------------- Laden / Speichern
async function load() {
  const r = await H.api('/api/settings');
  S.cfg = r.config; S.meta = r.meta; S.orig = JSON.stringify(S.cfg);
}

async function save(msg) {
  const btn = H.$('#sb-save'); if (btn) btn.disabled = true;
  try {
    const r = await H.api('/api/settings', { method: 'PUT', json: S.cfg });
    S.cfg = r.config; S.orig = JSON.stringify(S.cfg); S.meta.first_run = !Object.keys(S.cfg.cameras).length;
    H.toast(`<div><b>${H.esc(msg || 'Gespeichert')}</b><div style="color:var(--muted);font-size:12px">${r.changed.length ? 'Neu gestartet: ' + H.esc(r.changed.join(', ')) : 'Sofort aktiv'}</div></div>`);
    await H.reloadConfig();
    return true;
  } catch (e) {
    H.toast(`<div style="color:var(--danger)"><b>Nicht gespeichert</b><div style="font-size:12px">${H.esc(e.message)}</div></div>`);
    return false;
  } finally { updateBar(); if (btn) btn.disabled = false; }
}

function updateBar() {
  const bar = H.$('#savebar'); if (bar) bar.hidden = !S.cfg || !dirty();
}

// ---------------------------------------------------------------- Seite
export async function settingsPage(helpers, editId = null) {
  H = helpers;
  initAccount(helpers);
  const me = H.state.me;
  const canCfg = me.is_admin || me.perms.includes('settings');
  const tabs = [];
  if (canCfg) tabs.push(['cameras', 'Kameras'], ['detect', 'Erkennung'], ['faces', 'Gesichter'], ['record', 'Aufnahme & Speicher'], ['ha', 'Home Assistant'], ['access', 'Netzwerk']);
  if (me.is_admin) tabs.push(['users', 'Benutzer']);
  tabs.push(['account', 'Mein Konto']);
  if (canCfg) await load();
  if (!tabs.some(([k]) => k === S.tab)) S.tab = tabs[0][0];
  if (canCfg && S.meta.first_run && !Object.keys(S.cfg.cameras).length) S.tab = 'cameras';
  H.main.innerHTML = `
    <div class="page-head"><h1>${canCfg ? 'Einstellungen' : 'Mein Konto'}</h1><span class="spacer"></span>
      ${canCfg ? `<a class="btn small" href="/api/settings/export">Exportieren</a>
      <label class="btn small">Importieren<input type="file" accept=".yml,.yaml" hidden id="imp"></label>` : ''}</div>
    <div class="tabs wide" id="stabs">${tabs.map(([k, l]) => `<button data-t="${k}" class="${S.tab === k ? 'on' : ''}">${l}</button>`).join('')}</div>
    <div id="stab"></div>
    <div class="savebar" id="savebar" hidden><span>Ungespeicherte Änderungen</span><span class="spacer"></span>
      <button class="btn" id="sb-reset">Verwerfen</button><button class="btn primary" id="sb-save">Speichern & übernehmen</button></div>`;
  H.$$('#stabs button').forEach((b) => (b.onclick = () => { S.tab = b.dataset.t; H.$$('#stabs button').forEach((x) => x.classList.toggle('on', x === b)); renderTab(); }));
  H.$('#sb-save').onclick = () => save();
  H.$('#sb-reset').onclick = () => { S.cfg = JSON.parse(S.orig); renderTab(); updateBar(); };
  if (H.$('#imp')) H.$('#imp').onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    if (!confirm('Aktuelle Einstellungen durch die Datei ersetzen?')) return;
    const fd = new FormData(); fd.append('file', f);
    try { await H.api('/api/settings/import', { method: 'POST', body: fd }); H.toast('Konfiguration importiert'); await load(); await H.reloadConfig(); renderTab(); }
    catch (err) { H.toast(`<span style="color:var(--danger)">${H.esc(err.message)}</span>`); }
  };
  const beforeUnload = (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ''; } };
  window.addEventListener('beforeunload', beforeUnload);
  if (editId && S.cfg.cameras[editId]) S.tab = 'cameras';
  renderTab();
  if (editId && S.cfg.cameras[editId]) openCameraEditor(editId);
  return () => window.removeEventListener('beforeunload', beforeUnload);
}

function renderTab() {
  const el = H.$('#stab');
  const r = { cameras: tabCameras, detect: tabDetect, faces: tabFaces, record: tabRecord, ha: tabHA, access: tabAccess,
    users: usersTab, account: accountTab }[S.tab];
  H.$('#savebar').hidden = !['cameras', 'detect', 'faces', 'record', 'ha', 'access'].includes(S.tab) || !dirty();
  r(el);
}

const onChange = () => updateBar();

// ---------------------------------------------------------------- Kameras
async function tabCameras(el) {
  const cams = Object.entries(S.cfg.cameras);
  let stats = {};
  try { stats = (await H.api('/api/stats')).cameras; } catch {}
  if (S.tab !== 'cameras' || !el.isConnected) return;
  el.innerHTML = `
    ${!cams.length ? `<div class="welcome card"><img src="/static/icon.svg" alt=""><div><h2>Willkommen bei CamSpire</h2>
      <p>Füge deine erste Kamera hinzu. Am einfachsten über eine Hersteller-Vorlage – oder lass CamSpire per ONVIF im Netzwerk suchen.</p></div></div>` : ''}
    <div class="row-actions"><button class="btn primary" id="cam-add">${H.ICON.plus} Kamera hinzufügen</button>
      <button class="btn" id="cam-scan">Im Netzwerk suchen (ONVIF)</button></div>
    <div class="cam-list">${cams.map(([id, c]) => {
      const st = { online: 'online', offline: 'offline', connecting: 'verbindet', stalled: 'hängt', starting: 'startet' }[stats[id]?.status] || 'neu';
      return `<div class="cam-row" data-id="${id}">
        <img loading="lazy" src="/api/cameras/${id}/frame.jpg?w=320" onerror="this.style.visibility='hidden'" alt="">
        <div class="info"><div class="n">${H.esc(c.name)} <span class="pill ${st === 'online' ? 'ok' : st === 'neu' ? '' : 'bad'}">${st}</span></div>
          <div class="m">${H.esc(maskUrl(c.main))}</div>
          <div class="m">${c.sub ? 'Substream ✓ · ' : ''}${c.detect.fps} fps · ${Object.keys(c.zones || {}).length} Zonen · ${c.faces ? 'Gesichter · ' : ''}${{ events: 'Ereignis-Aufnahme', continuous: 'Daueraufnahme', off: 'keine Aufnahme' }[!c.record ? 'off' : (c.record_mode && c.record_mode !== 'default' ? c.record_mode : S.cfg.record.mode)]}${c.detect.enabled ? '' : ' · Erkennung aus'}</div></div>
        <button class="btn small">Bearbeiten</button></div>`;
    }).join('')}</div>`;
  H.$('#cam-add', el).onclick = () => openCameraEditor(null);
  H.$('#cam-scan', el).onclick = openOnvifScan;
  H.$$('.cam-row', el).forEach((r) => (r.onclick = () => openCameraEditor(r.dataset.id)));
}

function newCamera(name = '') {
  return { name, main: '', sub: null, detect: { stream: 'sub', width: null, height: null, fps: 5, enabled: true },
    hwaccel: 'cuda', faces: true, record: true, record_mode: 'default', events_days: null, continuous_days: null,
    live_transcode: 'auto', objects: null, zones: {}, require_zone: false, masks: [],
    motion: { threshold: 25, min_area: 0.002 } };
}

export function openCameraEditor(id, preset = null) {
  const isNew = !id;
  const cam = isNew ? Object.assign(newCamera(), preset || {}) : clone(S.cfg.cameras[id]);
  const st = { id: id || '', tpl: { brand: 'hikvision', ip: '', u: 'admin', p: '' }, useGlobalObjects: !cam.objects, testImg: null,
    res: { auto: !(cam.detect.width && cam.detect.height) } };
  const card = H.openModal(`
    <div class="m-head"><h2>${isNew ? 'Neue Kamera' : H.esc(cam.name)}</h2><span class="spacer"></span>
      ${isNew ? '' : `<button class="btn icon danger" id="c-del" title="Kamera löschen">${H.ICON.trash}</button>`}
      <button class="btn icon" data-close>${H.ICON.close}</button></div>
    <div class="m-body cam-editor">
      <section><h3>Allgemein</h3>
        ${row('Name', text('name', 'z. B. Einfahrt', 'text', 'cam'))}
        ${row('ID', `<input type="text" id="c-id" value="${H.esc(st.id)}" placeholder="wird aus dem Namen erzeugt" autocapitalize="none" spellcheck="false">`, isNew ? 'für MQTT/Home Assistant, a–z 0–9 _' : 'änderbar – Aufnahmen und Ereignisse werden übernommen, in Home Assistant entstehen neue Entitäten')}
      </section>
      <section><h3>Verbindung</h3>
        <div class="tpl card">
          <div class="tpl-grid">
            <label>Hersteller${select('brand', Object.entries(TEMPLATES).map(([k, t]) => [k, t.name]), 'tpl')}</label>
            <label>IP-Adresse${text('ip', '192.168.1.50', 'text', 'tpl')}</label>
            <label>Benutzer${text('u', 'admin', 'text', 'tpl')}</label>
            <label>Passwort${text('p', '', 'password', 'tpl')}</label>
          </div>
          <button class="btn small" id="c-tpl">Adressen erzeugen</button>
        </div>
        ${row('Hauptstream', `<div class="url">${text('main', 'rtsp://benutzer:passwort@ip:554/…', 'text', 'cam')}<button class="btn small" data-test="main">Testen</button></div><div class="test" id="t-main"></div>`, 'volle Auflösung – Aufnahme & Vollbild')}
        ${row('Substream', `<div class="url"><input type="text" id="c-sub" placeholder="optional, empfohlen" spellcheck="false"><button class="btn small" data-test="sub">Testen</button></div><div class="test" id="t-sub"></div>`, 'kleine Auflösung – Erkennung & Live-Raster')}
      </section>
      <section><h3>Erkennung</h3>
        ${row('Erkennung aktiv', toggle('detect.enabled', 'cam'))}
        ${row('Erkennungs-Stream', seg('detect.stream', [['sub', 'Substream'], ['main', 'Hauptstream']], 'cam'), 'Gesichter brauchen mind. 720p')}
        ${row('Bilder pro Sekunde', range('detect.fps', 1, 15, 1, 'fps', 'cam'), 'mehr = schnellere Reaktion, mehr GPU-Last')}
        ${row('Auflösung', `${toggle('auto', 'res')} <span class="muted">automatisch</span><div class="wh" id="c-wh">${num('detect.width', 320, 3840, 2, 'cam')} × ${num('detect.height', 240, 2160, 2, 'cam')}</div>`)}
        ${row('GPU-Dekodierung (NVDEC)', `<button type="button" class="switch" id="c-hw"></button>`)}
        ${row('Objekte', `${toggle('use', 'og')} <span class="muted">globale Auswahl verwenden</span><div id="c-objs"></div>`)}
        ${row('Gesichtserkennung', toggle('faces', 'cam'))}
        ${row('Bewegungsempfindlichkeit', range('motion.threshold', 5, 80, 1, 'v', 'cam'), 'kleiner = empfindlicher')}
        ${row('Mindestgröße Bewegung', range('motion.min_area', 0.0005, 0.02, 0.0005, 'pct1', 'cam'), 'Anteil des Bildes')}
      </section>
      <section><h3>Zonen & Masken</h3>
        <p class="muted" style="margin:0 0 10px">Zone/Maske links auswählen, dann ins Bild klicken, um Punkte zu setzen. Punkte lassen sich ziehen, Doppelklick entfernt einen Punkt. Masken blenden Bereiche aus (Bäume, Straße, Zeitstempel).</p>
        <div class="zone-editor" id="ze"></div>
        ${row('Nur Ereignisse in Zonen', toggle('require_zone', 'cam'), 'Objekte außerhalb aller Zonen lösen nichts aus')}
      </section>
      <section><h3>Aufnahme</h3>
        ${row('Aufnahmemodus', seg('record_mode', [['default', `Global (${{ events: 'Ereignisse', continuous: 'Dauer', off: 'aus' }[S.cfg.record.mode]})`], ['events', 'Nur Ereignisse'], ['continuous', 'Daueraufnahme'], ['off', 'Aus']], 'cam'))}
        ${row('Ereignisse behalten', `${num('events_days', 1, 3650, 1, 'cam')} <span class="muted">Tage – leer = global (${S.cfg.record.retention.events_days})</span>`)}
        ${row('Daueraufnahme behalten', `${num('continuous_days', 1, 3650, 1, 'cam')} <span class="muted">Tage – leer = global (${S.cfg.record.retention.continuous_days})</span>`)}
      </section>
      <section><h3>Live-Bild</h3>
        ${row('H.265 für den Browser umwandeln', seg('live_transcode', [['auto', 'Automatisch'], ['on', 'Immer'], ['off', 'Nie']], 'cam'), 'Viele Browser zeigen H.265 nicht an – CamSpire wandelt dann auf der GPU in H.264 um (nur solange jemand zuschaut)')}
      </section>
      <div class="m-foot"><span class="spacer"></span><button class="btn" data-close>Abbrechen</button><button class="btn primary" id="c-save">Speichern</button></div>
    </div>`, true);

  const og = { use: st.useGlobalObjects };
  // Zugangsdaten aus vorhandener Adresse in die Vorlage übernehmen
  const m = /^\w+:\/\/(?:([^:@/]+)(?::([^@/]*))?@)?([^:/?#]+)/.exec(cam.main || '');
  if (m) { st.tpl.u = decodeURIComponent(m[1] || 'admin'); st.tpl.p = decodeURIComponent(m[2] || ''); st.tpl.ip = m[3]; }
  const sync = () => {
    H.$('#c-wh', card).hidden = st.res.auto;
    if (!isNew) return;
    const idEl = H.$('#c-id', card);
    if (!idEl.dataset.touched) { idEl.value = slug(cam.name); st.id = idEl.value; }
  };
  bind(card, { cam, tpl: st.tpl, res: st.res, og }, (p) => {
    if (p === 'auto') { if (st.res.auto) { cam.detect.width = null; cam.detect.height = null; } else { cam.detect.width ||= 1280; cam.detect.height ||= 720; bind(H.$('#c-wh', card), { cam }, () => {}); } }
    if (p === 'use') { cam.objects = og.use ? null : clone(S.cfg.objects.track); renderObjs(); }
    sync();
  });
  const idEl = H.$('#c-id', card);
  idEl.oninput = () => { idEl.dataset.touched = 1; const p = idEl.selectionStart; idEl.value = idEl.value.toLowerCase().replace(/[^a-z0-9_]/g, '_'); idEl.setSelectionRange(p, p); st.id = idEl.value; };
  idEl.onblur = () => { idEl.value = slug(idEl.value) || (isNew ? slug(cam.name) : id); st.id = idEl.value; };
  const subEl = H.$('#c-sub', card);
  subEl.value = cam.sub || ''; subEl.oninput = () => { cam.sub = subEl.value.trim() || null; };
  const hw = H.$('#c-hw', card); hw.classList.toggle('on', cam.hwaccel === 'cuda');
  hw.onclick = () => { cam.hwaccel = cam.hwaccel === 'cuda' ? 'none' : 'cuda'; hw.classList.toggle('on', cam.hwaccel === 'cuda'); };
  const renderObjs = () => {
    const box = H.$('#c-objs', card);
    if (!cam.objects) { box.innerHTML = `<div class="muted" style="margin-top:6px">${S.cfg.objects.track.map((l) => LABEL_DE[l] || l).join(', ')}</div>`; return; }
    box.innerHTML = labelChips(cam.objects, null, S.meta.labels); bindChips(box, cam.objects, () => {});
  };
  renderObjs(); sync();

  H.$('#c-tpl', card).onclick = () => {
    const t = TEMPLATES[st.tpl.brand]; if (!st.tpl.ip) return H.toast('Bitte IP-Adresse eintragen');
    const fill = (s) => s.replace('{u}', encodeURIComponent(st.tpl.u || '')).replace('{p}', encodeURIComponent(st.tpl.p || '')).replace('{ip}', st.tpl.ip.trim()).replace('//:@', '//');
    cam.main = fill(t.main); cam.sub = t.sub ? fill(t.sub) : null;
    H.$('[data-bind="main"]', card).value = cam.main; subEl.value = cam.sub || '';
    if (!cam.sub) cam.detect.stream = 'main';
    bind(H.$('[data-bind="detect.stream"]', card).parentElement, { cam }, () => {});
    H.toast('Adressen erzeugt – jetzt „Testen“');
  };

  H.$$('[data-test]', card).forEach((b) => (b.onclick = async () => {
    const which = b.dataset.test; const url = which === 'main' ? cam.main : cam.sub;
    const out = H.$(`#t-${which}`, card);
    if (!url) { out.innerHTML = '<span class="muted">Keine Adresse eingetragen</span>'; return; }
    b.disabled = true; out.innerHTML = '<span class="muted">Verbinde …</span>';
    try {
      const r = await H.api('/api/settings/test-stream', { method: 'POST', json: { url } });
      if (!r.ok) { out.innerHTML = `<span class="bad">✕ ${H.esc(r.error)}</span>`; return; }
      out.innerHTML = `<span class="good">✓ ${String(r.codec).toUpperCase()} · ${r.width}×${r.height}${r.fps ? ` · ${r.fps} fps` : ''}${r.audio ? ` · Ton (${r.audio})` : ''}</span>${r.image ? `<img src="${r.image}">` : ''}`;
      if (r.image && (which === 'sub' || !st.testImg)) { st.testImg = r.image; ze.setImage(r.image); }
      if (which === 'sub' && r.height && r.height < 700 && cam.faces && cam.detect.stream === 'sub')
        out.innerHTML += `<div class="muted" style="margin-top:6px">Hinweis: Für Gesichtserkennung ist ${r.height}p knapp – ggf. Hauptstream für die Erkennung wählen.</div>`;
    } catch (e) { out.innerHTML = `<span class="bad">✕ ${H.esc(e.message)}</span>`; } finally { b.disabled = false; }
  }));

  const ze = zoneEditor(H.$('#ze', card), cam, isNew ? null : `/api/cameras/${id}/frame.jpg?w=1280&t=${Date.now()}`);

  const del = H.$('#c-del', card);
  if (del) del.onclick = async () => {
    if (!confirm(`Kamera „${cam.name}“ löschen? Aufnahmen bleiben erhalten, bis sie ablaufen.`)) return;
    delete S.cfg.cameras[id];
    if (await save('Kamera gelöscht')) { H.closeModal(); renderTab(); }
  };
  H.$('#c-save', card).onclick = async () => {
    if (!cam.name?.trim()) return H.toast('Bitte einen Namen eingeben');
    if (!cam.main?.trim()) return H.toast('Bitte die Adresse des Hauptstreams eintragen');
    let cid = isNew ? (st.id || slug(cam.name)) : id;
    const newId = slug(H.$('#c-id', card).value);
    if (!/^[a-z0-9_]+$/.test(newId || cid)) return H.toast('Ungültige ID');
    if (isNew && S.cfg.cameras[cid]) return H.toast('Diese ID gibt es schon');
    if (!isNew && newId && newId !== id) {
      if (S.cfg.cameras[newId]) return H.toast('Diese ID gibt es schon');
      if (!confirm(`ID von „${id}“ in „${newId}“ ändern? Aufnahmen, Ereignisse und Rechte werden übernommen. In Home Assistant heißen die Entitäten danach camspire_${newId}_…`)) return;
      try {
        await H.api(`/api/settings/cameras/${id}/rename`, { method: 'POST', json: { new_id: newId } });
        const keepDirty = clone(S.cfg); delete keepDirty.cameras[id];
        await load();
        Object.assign(S.cfg, { ...keepDirty, cameras: S.cfg.cameras });
        cid = newId;
      } catch (e) { return H.toast(`<span class="bad">${H.esc(e.message)}</span>`); }
    }
    if (!cam.sub && cam.detect.stream === 'sub') cam.detect.stream = 'main';
    cam.record = true;
    if (!cam.events_days) cam.events_days = null;
    if (!cam.continuous_days) cam.continuous_days = null;
    const backup = clone(S.cfg.cameras);
    S.cfg.cameras[cid] = cam;
    if (await save(isNew ? 'Kamera hinzugefügt' : 'Kamera gespeichert')) { H.closeModal(); renderTab(); }
    else S.cfg.cameras = backup;
  };
  card.onclose = () => ze.destroy();
}

// ---------------------------------------------------------------- Zonen-Editor
function zoneEditor(root, cam, imgUrl) {
  root.innerHTML = `
    <div class="ze-side">
      <div class="ze-list"></div>
      <div class="ze-add"><button class="btn small" data-add="zone">${H.ICON.plus} Zone</button><button class="btn small" data-add="mask">${H.ICON.plus} Maske</button></div>
    </div>
    <div class="ze-stage"><img alt=""><canvas></canvas><div class="ze-empty">Bild erscheint nach „Testen“ des Streams</div></div>`;
  const img = H.$('img', root), cv = H.$('canvas', root), ctx = cv.getContext('2d'), list = H.$('.ze-list', root);
  const empty = H.$('.ze-empty', root);
  // Arbeitsliste: Zonen (Name, Punkte, Objekte) und Masken
  const items = [
    ...Object.entries(cam.zones || {}).map(([name, z]) => ({ kind: 'zone', name, points: z.points.map((p) => [...p]), objects: [...(z.objects || [])] })),
    ...(cam.masks || []).map((m) => ({ kind: 'mask', points: m.map((p) => [...p]) })),
  ];
  let sel = items.length ? 0 : -1, drag = null;
  const commit = () => {
    cam.zones = {}; cam.masks = [];
    items.forEach((it) => {
      if (it.points.length < 3) return;
      const pts = it.points.map(([x, y]) => [Math.round(x * 1000) / 1000, Math.round(y * 1000) / 1000]);
      if (it.kind === 'zone') cam.zones[slug(it.name) || `zone_${Object.keys(cam.zones).length + 1}`] = { points: pts, objects: it.objects };
      else cam.masks.push(pts);
    });
  };
  const color = (i) => items[i].kind === 'mask' ? '#9aa3b8' : ZONE_COLORS[items.slice(0, i).filter((x) => x.kind === 'zone').length % ZONE_COLORS.length];
  const draw = () => {
    const w = img.clientWidth, h = img.clientHeight;
    if (!w) return;
    cv.width = w * devicePixelRatio; cv.height = h * devicePixelRatio; cv.style.width = w + 'px'; cv.style.height = h + 'px';
    ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0); ctx.clearRect(0, 0, w, h);
    items.forEach((it, i) => {
      if (!it.points.length) return;
      const c = color(i);
      ctx.beginPath(); it.points.forEach(([x, y], k) => (k ? ctx.lineTo(x * w, y * h) : ctx.moveTo(x * w, y * h)));
      ctx.closePath();
      ctx.fillStyle = it.kind === 'mask' ? 'rgba(10,14,26,.6)' : c + '33'; ctx.fill();
      ctx.lineWidth = i === sel ? 2.5 : 1.5; ctx.strokeStyle = c; ctx.setLineDash(it.kind === 'mask' ? [6, 4] : []); ctx.stroke(); ctx.setLineDash([]);
      if (i === sel) it.points.forEach(([x, y]) => { ctx.beginPath(); ctx.arc(x * w, y * h, 6, 0, 7); ctx.fillStyle = '#fff'; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = c; ctx.stroke(); });
      if (it.kind === 'zone' && it.points.length) { ctx.font = '600 13px Inter, sans-serif'; ctx.fillStyle = c; ctx.fillText(it.name, it.points[0][0] * w + 8, it.points[0][1] * h + 18); }
    });
  };
  const renderList = () => {
    list.innerHTML = items.length ? items.map((it, i) => `
      <div class="ze-item ${i === sel ? 'on' : ''}" data-i="${i}">
        <div class="ze-top"><i style="background:${color(i)}"></i>
          ${it.kind === 'zone' ? `<input type="text" value="${H.esc(it.name)}" data-name>` : '<span>Maske</span>'}
          <span class="muted">${it.points.length} Pkt.</span>
          <button class="btn icon small" data-clear title="Punkte löschen">↺</button><button class="btn icon small danger" data-del title="Entfernen">${H.ICON.trash}</button></div>
        ${it.kind === 'zone' && i === sel ? `<div class="ze-objs"><span class="muted">Objekte (leer = alle):</span><div class="chips">${['person', 'car', 'truck', 'motorcycle', 'bicycle', 'dog', 'cat', 'bird'].map((l) => `<span class="chip ${it.objects.includes(l) ? 'on' : ''}" data-l="${l}">${LABEL_DE[l]}</span>`).join('')}</div></div>` : ''}
      </div>`).join('') : '<div class="muted">Noch keine Zonen – die ganze Kamera wird überwacht.</div>';
    H.$$('.ze-item', list).forEach((el) => {
      const i = +el.dataset.i, it = items[i];
      el.onclick = (e) => { if (e.target.closest('input,button,.chip')) return; sel = i; renderList(); draw(); };
      const n = H.$('[data-name]', el); if (n) { n.onfocus = () => { if (sel !== i) { sel = i; draw(); } }; n.oninput = () => { it.name = n.value; draw(); }; n.onblur = () => { it.name = slug(it.name) || 'zone'; n.value = it.name; commit(); draw(); }; }
      H.$('[data-del]', el).onclick = () => { items.splice(i, 1); sel = Math.min(sel, items.length - 1); commit(); renderList(); draw(); };
      H.$('[data-clear]', el).onclick = () => { it.points = []; commit(); renderList(); draw(); };
      if (it.kind === 'zone') bindChips(el, it.objects, commit);
    });
  };
  H.$$('[data-add]', root).forEach((b) => (b.onclick = () => {
    const kind = b.dataset.add;
    const n = items.filter((x) => x.kind === 'zone').length + 1;
    items.push(kind === 'zone' ? { kind, name: n === 1 ? 'zone' : `zone_${n}`, points: [], objects: [] } : { kind, points: [] });
    sel = items.length - 1; renderList(); draw();
  }));
  const pos = (e) => { const r = cv.getBoundingClientRect(); return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))]; };
  const near = (p) => {
    if (sel < 0) return -1; const r = cv.getBoundingClientRect();
    return items[sel].points.findIndex(([x, y]) => Math.hypot((x - p[0]) * r.width, (y - p[1]) * r.height) < 11);
  };
  cv.onpointerdown = (e) => {
    if (sel < 0) { H.toast('Zuerst links „+ Zone“ oder „+ Maske“ wählen'); return; }
    const p = pos(e); const k = near(p);
    if (k >= 0) { drag = k; cv.setPointerCapture(e.pointerId); return; }
    const pts = items[sel].points;
    // Punkt in die nächstgelegene Kante einfügen (ab 3 Punkten), sonst anhängen
    let at = pts.length;
    if (pts.length >= 3) {
      let best = Infinity;
      pts.forEach((a, i) => { const b = pts[(i + 1) % pts.length]; const d = segDist(p, a, b); if (d < best) { best = d; at = i + 1; } });
      if (best > 0.03) at = pts.length;
    }
    pts.splice(at, 0, p); drag = at; cv.setPointerCapture(e.pointerId); commit(); renderList(); draw();
  };
  cv.onpointermove = (e) => { if (drag === null) { cv.style.cursor = near(pos(e)) >= 0 ? 'grab' : 'crosshair'; return; } items[sel].points[drag] = pos(e); draw(); };
  cv.onpointerup = () => { if (drag !== null) { drag = null; commit(); renderList(); } };
  cv.ondblclick = (e) => { const k = near(pos(e)); if (k >= 0) { items[sel].points.splice(k, 1); commit(); renderList(); draw(); } };
  const setImage = (src) => { if (!src) return; img.onload = () => { empty.hidden = true; draw(); }; img.src = src; };
  const ro = new ResizeObserver(draw); ro.observe(img);
  setImage(imgUrl);
  img.onerror = () => { empty.hidden = false; };
  renderList();
  return { setImage, destroy: () => ro.disconnect() };
}
function segDist(p, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1]; const l = dx * dx + dy * dy || 1e-9;
  const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l));
  return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
}

// ---------------------------------------------------------------- ONVIF-Suche
function openOnvifScan() {
  const card = H.openModal(`
    <div class="m-head"><h2>Kameras im Netzwerk suchen</h2><span class="spacer"></span><button class="btn icon" data-close>${H.ICON.close}</button></div>
    <div class="m-body">
      <p class="muted" style="margin:0">Sucht per ONVIF (WS-Discovery). Das funktioniert nur, wenn der Container im Host-Netzwerk läuft
        (<code>network_mode: host</code> in docker-compose.yml) – sonst Kamera über „Kamera hinzufügen“ mit Hersteller-Vorlage anlegen.</p>
      <div><button class="btn primary" id="o-scan">Suche starten</button></div>
      <div id="o-res"></div>
    </div>`);
  H.$('#o-scan', card).onclick = async () => {
    const res = H.$('#o-res', card); res.innerHTML = '<span class="muted">Suche läuft (ca. 5 s) …</span>';
    try {
      const r = await H.api('/api/settings/onvif');
      const devs = r.sources || [];
      if (!devs.length) { res.innerHTML = '<div class="empty">Keine ONVIF-Kameras gefunden</div>'; return; }
      res.innerHTML = devs.map((d, i) => `<div class="card onvif" data-i="${i}">
        <div class="n">${H.esc(d.info || d.name)}</div><div class="muted">${H.esc(d.name)}</div>
        <div class="tpl-grid" style="margin-top:10px"><label>Benutzer<input type="text" value="admin" data-u></label><label>Passwort<input type="password" data-p></label></div>
        <button class="btn small" data-load>Streams abrufen</button><div class="o-prof"></div></div>`).join('');
      H.$$('.onvif', res).forEach((el) => {
        const d = devs[+el.dataset.i];
        H.$('[data-load]', el).onclick = async () => {
          const u = encodeURIComponent(H.$('[data-u]', el).value), p = encodeURIComponent(H.$('[data-p]', el).value);
          const src = d.url.replace('user:pass@', `${u}:${p}@`);
          const prof = H.$('.o-prof', el); prof.innerHTML = '<span class="muted">Frage Kamera ab …</span>';
          try {
            const pr = (await H.api(`/api/settings/onvif?src=${encodeURIComponent(src)}`)).sources.filter((s) => !s.url.includes('snapshot'));
            if (!pr.length) { prof.innerHTML = '<span class="bad">Keine Streams</span>'; return; }
            prof.innerHTML = `<div class="muted" style="margin:8px 0">${pr.length} Profile gefunden – meist ist das erste der Haupt-, das zweite der Substream.</div>
              <button class="btn primary small" data-take>Als Kamera übernehmen</button>`;
            H.$('[data-take]', prof).onclick = () => {
              const host = (d.name || '').split(':')[0];
              openCameraEditor(null, { name: `Kamera ${host.split('.').pop() || ''}`.trim(), main: pr[0].url, sub: pr[1]?.url || null,
                detect: { stream: pr[1] ? 'sub' : 'main', width: null, height: null, fps: 5, enabled: true } });
            };
          } catch (e) { prof.innerHTML = `<span class="bad">✕ ${H.esc(e.message)}</span>`; }
        };
      });
    } catch (e) { res.innerHTML = `<span class="bad">✕ ${H.esc(e.message)}</span>`; }
  };
}

// ---------------------------------------------------------------- Erkennung
function tabDetect(el) {
  const c = S.cfg;
  const hasGpu = S.meta.providers.some((p) => p.includes('CUDA') || p.includes('Tensorrt'));
  el.innerHTML = `
    <div class="card form"><h3>Objekterkennung (YOLO)</h3>
      ${row('Recheneinheit', select('detector.device', [['auto', 'Automatisch (GPU wenn verfügbar)'], ['cuda', 'NVIDIA GPU (CUDA)'], ['cpu', 'Prozessor (CPU)']]), hasGpu ? 'GPU erkannt ✓' : 'Keine GPU sichtbar – läuft auf der CPU')}
      ${row('GPU-Nummer', num('detector.gpu_id', 0, 8), 'nur bei mehreren Grafikkarten')}
      ${row('Mindest-Sicherheit', range('detector.min_score', 0.2, 0.9, 0.01, 'pct'), 'globale Untergrenze für alle Objekte')}
      ${row('Bestätigung nach', range('objects.min_hits', 1, 8, 1, 'v'), 'so viele Treffer in Folge, bevor ein Ereignis entsteht')}
      ${row('Stillstand nach', range('objects.stationary_seconds', 10, 300, 5, 's'), 'z. B. geparktes Auto – danach endet das Ereignis')}
    </div>
    <div class="card form"><h3>Welche Objekte erkennen?</h3><div id="d-objs"></div></div>
    <div class="card form"><h3>Feinabstimmung pro Objekt</h3><p class="muted" style="margin-top:0">Höhere Sicherheit = weniger Fehlalarme. Mindestgröße in Pixeln (Breite × Höhe im Erkennungsbild) filtert z. B. weit entfernte Autos.</p><div id="d-filters"></div></div>`;
  bind(el, { cfg: c }, onChange);
  const renderObjs = () => {
    const box = H.$('#d-objs', el); box.innerHTML = labelChips(c.objects.track, null, S.meta.labels);
    bindChips(box, c.objects.track, () => { onChange(); renderFilters(); });
  };
  const renderFilters = () => {
    const box = H.$('#d-filters', el);
    box.innerHTML = `<table class="ftable"><tr><th>Objekt</th><th>Mindest-Sicherheit</th><th>Mindestgröße (px²)</th></tr>${c.objects.track.map((l) => {
      const f = c.objects.filters[l] || {};
      return `<tr data-l="${l}"><td class="ft-name">${H.esc(LABEL_DE[l] || l)}</td>
        <td data-label="Mindest-Sicherheit"><div class="range"><input type="range" min="0.2" max="0.95" step="0.01" value="${f.min_score ?? 0.5}" data-k="min_score"><output>${Math.round((f.min_score ?? 0.5) * 100)} %</output></div></td>
        <td data-label="Mindestgröße (px²)"><input type="number" min="0" step="100" value="${f.min_area ?? 0}" data-k="min_area" style="width:110px"></td></tr>`;
    }).join('')}</table>`;
    H.$$('tr[data-l] input', box).forEach((inp) => (inp.oninput = () => {
      const l = inp.closest('tr').dataset.l; const f = (c.objects.filters[l] ||= { min_score: 0.5, min_area: 0, max_area: 1e9 });
      f[inp.dataset.k] = Number(inp.value);
      const o = inp.parentElement.querySelector('output'); if (o) o.textContent = `${Math.round(inp.value * 100)} %`;
      onChange();
    }));
  };
  renderObjs(); renderFilters();
}

// ---------------------------------------------------------------- Gesichter
function tabFaces(el) {
  el.innerHTML = `
    <div class="card form"><h3>Gesichtserkennung</h3>
      ${row('Aktiv', toggle('faces.enabled'), S.meta.models.faces ? '' : 'Modelle fehlen im Image')}
      ${row('Erkennungs-Schwelle', range('faces.recognition_threshold', 0.3, 0.7, 0.01, 'v'), 'höher = strenger, weniger Verwechslungen, öfter „unbekannt“')}
      ${row('Minimale Gesichtsgröße', range('faces.min_face_size', 20, 120, 2, 'px'), 'kleinere Gesichter werden ignoriert')}
      ${row('Mindest-Bildqualität', range('faces.min_quality', 0.1, 0.8, 0.05, 'pct'), 'unscharfe/seitliche Gesichter verwerfen')}
    </div>
    <div class="card form"><h3>Lernen</h3>
      ${row('Automatisch dazulernen', toggle('faces.auto_learn'), 'sichere Treffer aus neuem Blickwinkel werden als Referenz gespeichert')}
      ${row('Schwelle fürs Lernen', range('faces.auto_learn_threshold', 0.4, 0.8, 0.01, 'v'), 'nur sehr sichere Treffer lernen')}
      ${row('Max. Referenzen pro Person', range('faces.max_samples_per_person', 10, 100, 5, 'v'))}
      ${row('Gruppierung Unbekannter', range('faces.unknown_cluster_threshold', 0.3, 0.7, 0.01, 'v'), 'höher = feinere Gruppen')}
    </div>`;
  bind(el, { cfg: S.cfg }, onChange);
}

// ---------------------------------------------------------------- Aufnahme
function tabRecord(el) {
  const st = S.meta.storage || {};
  el.innerHTML = `
    <div class="card form"><h3>Aufnahme</h3>
      ${row('Modus (Standard für alle Kameras)', seg('record.mode', [['events', 'Nur Ereignisse'], ['continuous', 'Daueraufnahme'], ['off', 'Aus']]), 'pro Kamera abweichend einstellbar. „Nur Ereignisse“ spart am meisten Speicher')}
      ${row('Qualität', seg('record.source', [['main', 'Hauptstream (voll)'], ['sub', 'Substream (klein)']]), 'Substream ≈ 1/10 der Größe')}
      ${row('Ton aufnehmen', toggle('record.audio'))}
      ${row('Vorlauf', range('record.pre_roll', 0, 30, 1, 's'), 'aus dem RAM-Puffer – kostet keinen Plattenplatz')}
      ${row('Nachlauf', range('record.post_roll', 0, 120, 1, 's'))}
    </div>
    <div class="card form"><h3>Speicherdauer</h3>
      <p class="muted" style="margin-top:0">Bei Daueraufnahme bleiben Abschnitte mit Ereignissen so lange wie Ereignisse, der Rest wird früher gelöscht. Pro Kamera abweichend einstellbar.</p>
      ${row('Ereignisse behalten', `${num('record.retention.events_days', 1, 3650)} <span class="muted">Tage</span>`, 'Aufnahmen + Snapshots von Ereignissen')}
      ${row('Daueraufnahme behalten', `${num('record.retention.continuous_days', 1, 3650)} <span class="muted">Tage</span>`, 'Aufnahmen ohne Ereignis')}
      ${row('Speicher-Obergrenze', `${num('record.retention.max_storage_gb', 1, 100000, 1)} <span class="muted">GB</span>`, 'wird sie erreicht, werden die ältesten Aufnahmen zuerst gelöscht')}
    </div>
    <div class="card form"><h3>Speicherort der Aufnahmen</h3>
      <div class="storage-status ${st.type === 'local' ? '' : st.ok ? 'ok' : 'bad'}">
        ${st.type === 'local' ? 'Aufnahmen liegen auf der lokalen Platte des Servers.'
          : st.ok ? `✓ Verbunden – Aufnahmen gehen auf <b>${H.esc(st.target)}</b>${st.usage ? ` · frei: ${(st.usage.free / 1e9).toFixed(0)} GB von ${(st.usage.total / 1e9).toFixed(0)} GB` : ''}`
          : `✕ ${H.esc(st.error || 'nicht verbunden')} – es wird vorübergehend lokal gespeichert`}</div>
      ${row('Ziel', seg('storage.type', [['local', 'Lokal'], ['smb', 'Netzwerkfestplatte (SMB)'], ['nfs', 'NFS'], ['path', 'Ordner']]))}
      <div id="st-fields"></div>
      <p class="muted" style="margin-bottom:0">Datenbank, Snapshots und Gesichter bleiben lokal (schnell und ausfallsicher). Ist das NAS kurz weg, speichert CamSpire lokal weiter und verbindet sich automatisch neu.</p>
    </div>
    <div class="card form"><h3>Snapshots</h3>
      ${row('JPEG-Qualität', range('snapshots.quality', 40, 95, 5, 'v'))}
      ${row('Max. Breite', range('snapshots.max_width', 640, 3840, 64, 'px'))}
    </div>`;
  const fields = () => {
    const t = S.cfg.storage.type, box = H.$('#st-fields', el);
    if (t === 'local') { box.innerHTML = ''; return; }
    box.innerHTML = (t === 'smb' ? `
      ${row('NAS-Adresse', text('storage.server', '192.168.1.5'))}
      ${row('Freigabe', text('storage.share', 'z. B. surveillance oder video/camspire'), 'Synology: Name des freigegebenen Ordners')}
      ${row('Benutzer', text('storage.username', 'NAS-Benutzer'))}
      ${row('Passwort', text('storage.password', '', 'password'))}
      ${row('SMB-Version', seg('storage.smb_version', [['3.0', '3.0'], ['2.1', '2.1'], ['2.0', '2.0'], ['1.0', '1.0 (alt)']]), 'Synology/QNAP/Windows: 3.0, FRITZ!Box: 3.0 oder 2.0')}`
      : t === 'nfs' ? `
      ${row('NAS-Adresse', text('storage.server', '192.168.1.5'))}
      ${row('Export-Pfad', text('storage.share', '/volume1/surveillance'), 'auf dem NAS die IP des Servers für NFS freigeben')}`
      : `${row('Ordner im Container', text('storage.path', '/nas'), 'auf dem Host eingebunden und in docker-compose.yml als Volume angegeben (z. B. /mnt/nas:/nas)')}`) + `
      ${row('Unterordner', text('storage.subfolder', 'camspire'))}
      ${t !== 'path' ? row('Weitere Mount-Optionen', text('storage.options', 'optional, z. B. seal'), 'für Fortgeschrittene') : ''}
      ${row('', '<button class="btn small" id="st-test">Verbindung testen</button> <span id="st-res"></span>')}`;
    bind(box, { cfg: S.cfg }, onChange);
    H.$('#st-test', box).onclick = async () => {
      const out = H.$('#st-res', box); out.innerHTML = '<span class="muted">teste … (bis zu 30 s)</span>';
      try {
        const r = await H.api('/api/settings/test-storage', { method: 'POST', json: S.cfg.storage });
        out.innerHTML = `<span class="${r.ok ? 'good' : 'bad'}">${r.ok ? '✓' : '✕'} ${H.esc(r.message)}${r.usage ? ` · ${(r.usage.free / 1e9).toFixed(0)} GB frei` : ''}</span>`;
      } catch (e) { out.innerHTML = `<span class="bad">✕ ${H.esc(e.message)}</span>`; }
    };
  };
  bind(el, { cfg: S.cfg }, (p) => { onChange(); if (p === 'storage.type') fields(); });
  fields();
}

// ---------------------------------------------------------------- Home Assistant / MQTT
function tabHA(el) {
  const p = () => S.cfg.mqtt.topic_prefix || 'camspire';
  el.innerHTML = `
    <div class="card form"><h3>MQTT-Broker</h3>
      ${row('MQTT aktiv', toggle('mqtt.enabled'))}
      ${row('Server', text('mqtt.host', '192.168.1.10 oder core-mosquitto'))}
      ${row('Port', num('mqtt.port', 1, 65535))}
      ${row('Benutzer', text('mqtt.username', 'optional'))}
      ${row('Passwort', text('mqtt.password', 'optional', 'password'))}
      ${row('', `<button class="btn small" id="m-test">Verbindung testen</button> <span id="m-res"></span>`)}
    </div>
    <div class="card form"><h3>Topics</h3>
      ${row('Topic-Präfix', text('mqtt.topic_prefix', 'camspire'))}
      ${row('Discovery-Präfix', text('mqtt.discovery_prefix', 'homeassistant'), 'Standard in Home Assistant: homeassistant')}
    </div>
    <div class="card"><h3>Was in Home Assistant erscheint</h3>
      <p class="muted" style="margin-top:0">Pro Kamera ein Gerät „CamSpire &lt;Name&gt;“ – automatisch per MQTT-Discovery:</p>
      <ul class="ha-list"><li><b>Person / Fahrzeug / Tier erkannt</b> – Belegungssensor, auch pro Zone</li>
        <li><b>Anzahl</b> Personen, Fahrzeuge, Tiere</li><li><b>Erkanntes Gesicht</b> – Name + Sicherheit</li>
        <li><b>Letzter Snapshot</b> als Kamera-Entität</li><li><b>Erkennung</b> an/aus als Schalter</li></ul>
      <p class="muted">Alle Ereignisse als JSON: <code id="m-topic"></code> · Live-Bild in HA: <code>rtsp://${H.esc(location.hostname)}:8554/&lt;kamera-id&gt;</code></p>
    </div>`;
  const upd = () => { H.$('#m-topic', el).textContent = `${p()}/events`; };
  bind(el, { cfg: S.cfg }, () => { onChange(); upd(); }); upd();
  H.$('#m-test', el).onclick = async () => {
    const m = S.cfg.mqtt; const out = H.$('#m-res', el); out.innerHTML = '<span class="muted">teste …</span>';
    try { const r = await H.api('/api/settings/test-mqtt', { method: 'POST', json: { host: m.host, port: m.port || 1883, username: m.username || '', password: m.password || '' } });
      out.innerHTML = `<span class="${r.ok ? 'good' : 'bad'}">${r.ok ? '✓' : '✕'} ${H.esc(r.message)}</span>`;
    } catch (e) { out.innerHTML = `<span class="bad">✕ ${H.esc(e.message)}</span>`; }
  };
}

// ---------------------------------------------------------------- Zugang & Netzwerk
function tabAccess(el) {
  const cand = { v: (S.cfg.webrtc.candidates || []).join(', ') };
  el.innerHTML = `
    <div class="card form"><h3>WebRTC (Live-Bild mit minimaler Verzögerung)</h3>
      <p class="muted" style="margin-top:0">IP und Port, unter denen der Browser den Server erreicht. Port 8555 (TCP + UDP) muss freigegeben sein. Ohne Eintrag läuft das Live-Bild über MSE (etwas mehr Verzögerung).</p>
      ${row('Adressen', `<input type="text" id="a-cand" placeholder="192.168.1.20:8555" spellcheck="false"> <button class="btn small" id="a-own">Diese Server-IP</button>`, 'mehrere mit Komma trennen')}
    </div>
    <div class="card"><h3>Konfigurationsdatei</h3>
      <p class="muted" style="margin:0">Alle Einstellungen liegen in <code>${H.esc(S.meta.config_path)}</code> (vorheriger Stand als <code>.bak</code>). Exportieren/Importieren oben rechts.</p></div>`;
  bind(el, { cfg: S.cfg }, onChange);
  const inp = H.$('#a-cand', el); inp.value = cand.v;
  const set = () => { S.cfg.webrtc.candidates = inp.value.split(',').map((x) => x.trim()).filter(Boolean); onChange(); };
  inp.oninput = set;
  H.$('#a-own', el).onclick = () => { const ip = `${location.hostname}:8555`; if (!inp.value.includes(ip)) inp.value = inp.value ? `${inp.value}, ${ip}` : ip; set(); };
}
