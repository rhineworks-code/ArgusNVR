// CamSpire NVR – Anmeldung, eigenes Konto, Benutzerverwaltung, Passkeys, Push, App-Installation
let H;
export function initAccount(h) { H = h; }

const PRESETS = {
  'Nur ansehen': ['live', 'events'],
  'Familie': ['live', 'history', 'events', 'download', 'events_manage'],
  'Alles außer Einstellungen': ['live', 'history', 'events', 'download', 'events_manage', 'faces', 'detect_toggle', 'system'],
};
const GROUP_DE = { person: 'Personen', vehicle: 'Fahrzeuge', animal: 'Tiere' };
const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleString('de-DE', { dateStyle: 'short', timeStyle: 'short' }) : '–');
const isStandalone = () => window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
const isIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const passkeysPossible = () => !!window.PublicKeyCredential && window.isSecureContext;

// ---------------------------------------------------------------- base64url
const b64uToBuf = (s) => { s = s.replace(/-/g, '+').replace(/_/g, '/'); while (s.length % 4) s += '='; return Uint8Array.from(atob(s), (c) => c.charCodeAt(0)).buffer; };
const bufToB64u = (b) => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

function creationOptions(o) {
  if (PublicKeyCredential.parseCreationOptionsFromJSON) return PublicKeyCredential.parseCreationOptionsFromJSON(o);
  return { ...o, challenge: b64uToBuf(o.challenge), user: { ...o.user, id: b64uToBuf(o.user.id) },
    excludeCredentials: (o.excludeCredentials || []).map((c) => ({ ...c, id: b64uToBuf(c.id) })) };
}
function requestOptions(o) {
  if (PublicKeyCredential.parseRequestOptionsFromJSON) return PublicKeyCredential.parseRequestOptionsFromJSON(o);
  return { ...o, challenge: b64uToBuf(o.challenge), allowCredentials: (o.allowCredentials || []).map((c) => ({ ...c, id: b64uToBuf(c.id) })) };
}
function credToJSON(c) {
  try { if (c.toJSON) return c.toJSON(); } catch {}
  const r = c.response; const out = { id: c.id, rawId: bufToB64u(c.rawId), type: c.type, clientExtensionResults: {}, authenticatorAttachment: c.authenticatorAttachment, response: { clientDataJSON: bufToB64u(r.clientDataJSON) } };
  if (r.attestationObject) { out.response.attestationObject = bufToB64u(r.attestationObject); out.response.transports = r.getTransports ? r.getTransports() : []; }
  if (r.authenticatorData) { out.response.authenticatorData = bufToB64u(r.authenticatorData); out.response.signature = bufToB64u(r.signature); out.response.userHandle = r.userHandle ? bufToB64u(r.userHandle) : null; }
  return out;
}
function deviceName() {
  const ua = navigator.userAgent;
  const os = /android/i.test(ua) ? 'Android' : isIOS() ? (/ipad/i.test(ua) ? 'iPad' : 'iPhone') : /windows/i.test(ua) ? 'Windows' : /mac os/i.test(ua) ? 'Mac' : /linux/i.test(ua) ? 'Linux' : 'Gerät';
  const br = /edg\//i.test(ua) ? 'Edge' : /firefox/i.test(ua) ? 'Firefox' : /chrome|crios/i.test(ua) ? 'Chrome' : /safari/i.test(ua) ? 'Safari' : '';
  return `${os}${br ? ' · ' + br : ''}${isStandalone() ? ' (App)' : ''}`;
}

// ---------------------------------------------------------------- Anmelde-/Einrichtungsbildschirm
export function renderAuthScreen(status) {
  const setup = status.setup_required;
  const pk = !setup && passkeysPossible();
  document.body.classList.add('auth-mode');
  const root = document.createElement('div'); root.className = 'auth';
  root.innerHTML = `
    <form class="auth-card" autocomplete="on">
      <div class="auth-brand"><img src="/static/icon.svg" alt=""><div><b>CamSpire</b><span>NVR</span></div></div>
      <h1>${setup ? 'Willkommen!' : 'Anmelden'}</h1>
      <p class="muted">${setup ? 'Lege das erste Konto an – es wird Administrator.' : 'Melde dich an, um deine Kameras zu sehen.'}</p>
      ${pk ? `<button type="button" class="btn primary wide" id="a-pk"><svg viewBox="0 0 24 24"><path d="M12 11c0 3-1 6-3 8M8 7.5A5 5 0 0 1 17 10c0 2-.3 4-1 6M5 10a7 7 0 0 1 2-5M19.5 13c-.2 2-.6 4-1.3 5.5M12 3a7 7 0 0 1 7 7"/></svg> Mit Fingerabdruck / Face ID</button>
        <div class="auth-or"><span>oder mit Passwort</span></div>` : ''}
      <label>Benutzername<input type="text" id="a-user" autocomplete="username" autocapitalize="none" required></label>
      ${setup ? '<label>Anzeigename<input type="text" id="a-name" placeholder="optional"></label>' : ''}
      <label>Passwort<input type="password" id="a-pass" autocomplete="${setup ? 'new-password' : 'current-password'}" required></label>
      ${setup ? '<label>Passwort wiederholen<input type="password" id="a-pass2" autocomplete="new-password" required></label>' : ''}
      ${setup ? '' : '<label class="check"><input type="checkbox" id="a-rem" checked> Angemeldet bleiben</label>'}
      <div class="auth-err" id="a-err"></div>
      <button type="submit" class="btn ${pk ? '' : 'primary'} wide" id="a-go">${setup ? 'Konto anlegen' : 'Anmelden'}</button>
      ${!setup && !window.isSecureContext ? '<p class="muted small">Anmeldung per Fingerabdruck/Face ID ist nur über HTTPS möglich.</p>' : ''}
    </form>`;
  document.body.append(root);
  const err = (m) => { root.querySelector('#a-err').textContent = m || ''; };
  root.querySelector('form').onsubmit = async (e) => {
    e.preventDefault(); err('');
    const username = root.querySelector('#a-user').value.trim(), password = root.querySelector('#a-pass').value;
    try {
      if (setup) {
        if (password !== root.querySelector('#a-pass2').value) return err('Die Passwörter stimmen nicht überein');
        await H.api('/api/auth/setup', { method: 'POST', json: { username, password, display_name: root.querySelector('#a-name').value } });
      } else {
        await H.api('/api/auth/login', { method: 'POST', json: { username, password, remember: root.querySelector('#a-rem').checked } });
      }
      location.reload();
    } catch (ex) { err(ex.message); }
  };
  const pkBtn = root.querySelector('#a-pk');
  if (pkBtn) pkBtn.onclick = async () => {
    err('');
    try {
      const { challenge_id, options } = await H.api('/api/auth/passkey/options', { method: 'POST' });
      const cred = await navigator.credentials.get({ publicKey: requestOptions(options) });
      await H.api('/api/auth/passkey/verify', { method: 'POST', json: { challenge_id, credential: credToJSON(cred), remember: root.querySelector('#a-rem').checked } });
      location.reload();
    } catch (ex) { err(ex.name === 'NotAllowedError' ? 'Abgebrochen' : ex.message); }
  };
  setTimeout(() => root.querySelector('#a-user').focus(), 50);
}

export async function logout() {
  try { await H.api('/api/auth/logout', { method: 'POST' }); } catch {}
  location.reload();
}

// ---------------------------------------------------------------- App-Installation
let installPrompt = null;
window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); installPrompt = e; document.dispatchEvent(new Event('camspire-installable')); });
export function registerSW() {
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch((e) => console.warn('SW', e));
  navigator.serviceWorker.addEventListener('message', (e) => {
    if (e.data?.type === 'navigate') { const u = new URL(e.data.url); location.hash = u.hash || '#/live'; }
  });
}

// ---------------------------------------------------------------- Push
const pushSupported = () => 'serviceWorker' in navigator && 'PushManager' in window && window.isSecureContext;
async function currentSub() {
  if (!pushSupported()) return null;
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}
async function enablePush() {
  if (Notification.permission === 'denied') throw new Error('Benachrichtigungen sind für diese Seite blockiert – in den Browser-Einstellungen erlauben');
  const perm = await Notification.requestPermission();
  if (perm !== 'granted') throw new Error('Benachrichtigungen wurden nicht erlaubt');
  const { key } = await H.api('/api/push/key');
  const reg = await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: new Uint8Array(b64uToBuf(key)) });
  await H.api('/api/push/subs', { method: 'POST', json: { subscription: sub.toJSON(), name: deviceName() } });
}

// ---------------------------------------------------------------- Mein Konto
export async function accountTab(el) {
  const me = await H.api('/api/me');
  el.innerHTML = `
    <div class="card form"><h3>Profil</h3>
      <div class="f-row"><div class="f-label">Benutzername</div><div class="f-ctl"><b>${H.esc(me.username)}</b> <span class="pill">${me.is_admin ? 'Admin' : 'Benutzer'}</span></div></div>
      <div class="f-row"><div class="f-label">Anzeigename</div><div class="f-ctl"><input type="text" id="m-name" value="${H.esc(me.display_name)}"><button class="btn small" id="m-name-save">Speichern</button></div></div>
      <div class="f-row"><div class="f-label">Passwort ändern</div><div class="f-ctl col">
        <input type="password" id="m-old" placeholder="Aktuelles Passwort" autocomplete="current-password">
        <input type="password" id="m-new" placeholder="Neues Passwort (mind. 8 Zeichen)" autocomplete="new-password">
        <input type="password" id="m-new2" placeholder="Neues Passwort wiederholen" autocomplete="new-password">
        <button class="btn small" id="m-pw">Passwort ändern</button></div></div>
      <div class="f-row"><div class="f-label"></div><div class="f-ctl"><button class="btn danger" id="m-logout">Abmelden</button></div></div>
    </div>
    <div class="card form"><h3>Anmelden mit Fingerabdruck / Face ID</h3>
      <p class="muted" style="margin-top:0">Passkeys ersetzen das Passwort: Anmeldung per Fingerabdruck, Gesichtserkennung, Windows Hello oder PIN des Geräts.
        Ein Passkey gilt für die Adresse, unter der du CamSpire gerade öffnest (<b>${H.esc(location.hostname)}</b>).</p>
      <div id="m-pks"></div>
      ${passkeysPossible() ? '<button class="btn primary small" id="m-pk-add">Passkey auf diesem Gerät hinzufügen</button>'
        : '<div class="storage-status bad">Nur über HTTPS möglich – öffne CamSpire über deine Domain (z. B. https://camspire-nvr.lws88.de).</div>'}
    </div>
    <div class="card form"><h3>Benachrichtigungen</h3><div id="m-push"></div></div>
    <div class="card form"><h3>App installieren</h3><div id="m-install"></div></div>
    <div class="card form"><h3>Angemeldete Geräte</h3><div id="m-sess"></div></div>
    <div class="card form"><h3>API-Tokens (z. B. für Home Assistant)</h3>
      <p class="muted" style="margin-top:0">Für Zugriffe ohne Anmeldung, z. B. Standbild in Home Assistant: <code>/api/cameras/&lt;id&gt;/frame.jpg?token=…</code>. Ein Token hat dieselben Rechte wie du.</p>
      <div id="m-tok"></div><div class="row-actions"><input type="text" id="m-tok-name" placeholder="Name, z. B. Home Assistant"><button class="btn small" id="m-tok-add">Token erstellen</button></div>
    </div>`;
  H.$('#m-name-save', el).onclick = async () => { await H.api('/api/me', { method: 'PUT', json: { display_name: H.$('#m-name', el).value } }); H.toast('Gespeichert'); H.reloadConfig(); };
  H.$('#m-pw', el).onclick = async () => {
    const n = H.$('#m-new', el).value;
    if (n !== H.$('#m-new2', el).value) return H.toast('Die neuen Passwörter stimmen nicht überein');
    try { await H.api('/api/me', { method: 'PUT', json: { password: n, current_password: H.$('#m-old', el).value } }); H.toast('Passwort geändert'); ['#m-old', '#m-new', '#m-new2'].forEach((s) => (H.$(s, el).value = '')); }
    catch (e) { H.toast(`<span class="bad">${H.esc(e.message)}</span>`); }
  };
  H.$('#m-logout', el).onclick = logout;

  // Passkeys
  const loadPk = async () => {
    const list = await H.api('/api/me/passkeys');
    H.$('#m-pks', el).innerHTML = list.length ? `<div class="list">${list.map((p) => `<div class="li"><div><b>${H.esc(p.name)}</b><small>angelegt ${fmtDate(p.created)} · zuletzt ${fmtDate(p.last_used)}${p.rp_id !== location.hostname ? ` · für ${H.esc(p.rp_id)}` : ''}</small></div><button class="btn icon small danger" data-id="${p.id}">${H.ICON.trash}</button></div>`).join('')}</div>`
      : '<p class="muted">Noch kein Passkey eingerichtet.</p>';
    H.$$('#m-pks [data-id]', el).forEach((b) => (b.onclick = async () => { if (!confirm('Passkey entfernen?')) return; await H.api(`/api/me/passkeys/${b.dataset.id}`, { method: 'DELETE' }); loadPk(); }));
  };
  loadPk();
  const pkAdd = H.$('#m-pk-add', el);
  if (pkAdd) pkAdd.onclick = async () => {
    try {
      const { challenge_id, options } = await H.api('/api/me/passkeys/options', { method: 'POST' });
      const cred = await navigator.credentials.create({ publicKey: creationOptions(options) });
      await H.api('/api/me/passkeys', { method: 'POST', json: { challenge_id, credential: credToJSON(cred), name: deviceName() } });
      H.toast('Passkey eingerichtet – ab jetzt per Fingerabdruck/Face ID anmelden'); loadPk();
    } catch (e) { if (e.name !== 'NotAllowedError') H.toast(`<span class="bad">${H.esc(e.message)}</span>`); }
  };

  renderPush(H.$('#m-push', el));
  renderInstall(H.$('#m-install', el));

  // Sitzungen
  const loadSess = async () => {
    const s = await H.api('/api/me/sessions');
    H.$('#m-sess', el).innerHTML = `<div class="list">${s.map((x) => `<div class="li"><div><b>${H.esc(uaShort(x.user_agent))}</b>${x.current ? ' <span class="pill ok">dieses Gerät</span>' : ''}<small>${H.esc(x.ip || '')} · ${x.method === 'passkey' ? 'Passkey' : 'Passwort'} · zuletzt aktiv ${fmtDate(x.last_seen)}</small></div>${x.current ? '' : `<button class="btn small" data-s="${x.id}">Abmelden</button>`}</div>`).join('')}</div>`;
    H.$$('#m-sess [data-s]', el).forEach((b) => (b.onclick = async () => { await H.api(`/api/me/sessions/${b.dataset.s}`, { method: 'DELETE' }); loadSess(); }));
  };
  loadSess();

  // Tokens
  const loadTok = async () => {
    const t = await H.api('/api/me/tokens');
    H.$('#m-tok', el).innerHTML = t.length ? `<div class="list">${t.map((x) => `<div class="li"><div><b>${H.esc(x.name)}</b><small>${H.esc(x.prefix)}… · erstellt ${fmtDate(x.created)} · zuletzt ${fmtDate(x.last_used)}</small></div><button class="btn icon small danger" data-t="${x.id}">${H.ICON.trash}</button></div>`).join('')}</div>` : '';
    H.$$('#m-tok [data-t]', el).forEach((b) => (b.onclick = async () => { if (!confirm('Token löschen? Dienste, die ihn nutzen, verlieren den Zugriff.')) return; await H.api(`/api/me/tokens/${b.dataset.t}`, { method: 'DELETE' }); loadTok(); }));
  };
  loadTok();
  H.$('#m-tok-add', el).onclick = async () => {
    const r = await H.api('/api/me/tokens', { method: 'POST', json: { name: H.$('#m-tok-name', el).value || 'Token' } });
    H.openModal(`<div class="m-head"><h2>Neuer API-Token</h2><span class="spacer"></span><button class="btn icon" data-close>${H.ICON.close}</button></div>
      <div class="m-body"><p>Jetzt kopieren – der Token wird nur einmal angezeigt:</p><input type="text" readonly value="${H.esc(r.token)}" id="tokv" style="width:100%;font-family:monospace">
      <button class="btn primary" id="tokc">Kopieren</button></div>`);
    H.$('#tokc').onclick = () => { navigator.clipboard?.writeText(r.token); H.toast('Kopiert'); };
    H.$('#tokv').select(); loadTok();
  };
}

function uaShort(ua = '') {
  const os = /android/i.test(ua) ? 'Android' : /iphone/i.test(ua) ? 'iPhone' : /ipad/i.test(ua) ? 'iPad' : /windows/i.test(ua) ? 'Windows' : /mac os/i.test(ua) ? 'Mac' : /linux/i.test(ua) ? 'Linux' : 'Unbekannt';
  const br = /edg\//i.test(ua) ? 'Edge' : /firefox/i.test(ua) ? 'Firefox' : /chrome|crios/i.test(ua) ? 'Chrome' : /safari/i.test(ua) ? 'Safari' : /python|curl|home ?assistant/i.test(ua) ? 'Programm' : '';
  return br ? `${os} · ${br}` : os;
}

async function renderPush(box) {
  if (!pushSupported()) {
    box.innerHTML = isIOS() && !isStandalone()
      ? '<div class="storage-status">Auf iPhone/iPad funktionieren Benachrichtigungen nur in der installierten App: unten „App installieren“ befolgen und CamSpire dann vom Home-Bildschirm öffnen.</div>'
      : !window.isSecureContext ? '<div class="storage-status bad">Benachrichtigungen brauchen HTTPS – öffne CamSpire über deine Domain.</div>'
        : '<div class="storage-status bad">Dieser Browser unterstützt keine Push-Benachrichtigungen.</div>';
    return;
  }
  const sub = await currentSub();
  const subs = await H.api('/api/push/subs');
  const mine = sub ? subs.find((s) => s.endpoint === sub.endpoint) : null;
  const others = subs.filter((s) => s !== mine);
  const cams = H.state.cams;
  box.innerHTML = mine ? `
      <div class="storage-status ok">✓ Benachrichtigungen auf diesem Gerät aktiv (${H.esc(mine.name)})</div>
      <div id="pp"></div>
      <div class="row-actions" style="margin-top:10px"><button class="btn small" id="p-test">Test senden</button><button class="btn small danger" id="p-off">Auf diesem Gerät ausschalten</button></div>`
    : `<p class="muted" style="margin-top:0">Erhalte eine Nachricht mit Bild, sobald jemand erkannt wird – auch wenn die App geschlossen ist.</p>
      <button class="btn primary" id="p-on">Auf diesem Gerät aktivieren</button>`;
  if (others.length) box.innerHTML += `<h4 class="sub-h">Weitere Geräte</h4><div class="list">${others.map((s) => `<div class="li"><div><b>${H.esc(s.name || 'Gerät')}</b><small>seit ${fmtDate(s.created)}${s.last_error ? ` · <span class="bad">Fehler: ${H.esc(s.last_error)}</span>` : s.last_ok ? ` · zuletzt ${fmtDate(s.last_ok)}` : ''}</small></div><button class="btn icon small danger" data-d="${s.id}">${H.ICON.trash}</button></div>`).join('')}</div>`;
  H.$$('[data-d]', box).forEach((b) => (b.onclick = async () => { await H.api(`/api/push/subs/${b.dataset.d}`, { method: 'DELETE' }); renderPush(box); }));
  const on = H.$('#p-on', box);
  if (on) on.onclick = async () => { on.disabled = true; try { await enablePush(); H.toast('Benachrichtigungen aktiviert'); } catch (e) { H.toast(`<span class="bad">${H.esc(e.message)}</span>`); } renderPush(box); };
  if (!mine) return;
  H.$('#p-test', box).onclick = async () => { const r = await H.api('/api/push/test', { method: 'POST', json: { id: mine.id } }); H.toast(H.esc(r.message)); };
  H.$('#p-off', box).onclick = async () => { await H.api(`/api/push/subs/${mine.id}`, { method: 'DELETE' }); try { await sub.unsubscribe(); } catch {} renderPush(box); };
  const p = mine.prefs;
  const save = async () => { await H.api(`/api/push/subs/${mine.id}`, { method: 'PATCH', json: { prefs: p } }); };
  const pp = H.$('#pp', box);
  const render = () => {
    pp.innerHTML = `
      <div class="f-row"><div class="f-label">Aktiv</div><div class="f-ctl"><button class="switch ${p.enabled ? 'on' : ''}" data-k="enabled"></button></div></div>
      <div class="f-row"><div class="f-label">Kameras</div><div class="f-ctl"><div class="chips">
        <span class="chip ${p.cameras === null ? 'on' : ''}" data-all>Alle</span>
        ${cams.map((c) => `<span class="chip ${p.cameras && p.cameras.includes(c.id) ? 'on' : ''}" data-cam="${c.id}">${H.esc(c.name)}</span>`).join('')}</div></div></div>
      <div class="f-row"><div class="f-label">Bei</div><div class="f-ctl"><div class="chips">
        ${Object.entries(GROUP_DE).map(([g, l]) => `<span class="chip ${p.groups.includes(g) ? 'on' : ''}" data-g="${g}">${l}</span>`).join('')}</div></div></div>
      <div class="f-row"><div class="f-label">Gesichter<small>bei Personen</small></div><div class="f-ctl"><div class="seg">
        ${[['all', 'Alle Personen'], ['unknown', 'Nur Unbekannte'], ['known', 'Nur Bekannte']].map(([v, l]) => `<button data-f="${v}" class="${p.faces === v ? 'on' : ''}">${l}</button>`).join('')}</div></div></div>
      <div class="f-row"><div class="f-label">Pause zwischen Meldungen<small>pro Kamera und Objektart</small></div><div class="f-ctl"><div class="range"><input type="range" min="0" max="900" step="15" value="${p.cooldown}" data-cd><output>${p.cooldown} s</output></div></div></div>
      <div class="f-row"><div class="f-label">Ruhezeit<small>keine Meldungen in diesem Zeitraum</small></div><div class="f-ctl"><input type="time" value="${p.quiet_from || ''}" data-q="quiet_from"> bis <input type="time" value="${p.quiet_to || ''}" data-q="quiet_to"></div></div>`;
    H.$('[data-k="enabled"]', pp).onclick = () => { p.enabled = !p.enabled; save(); render(); };
    H.$('[data-all]', pp).onclick = () => { p.cameras = null; save(); render(); };
    H.$$('[data-cam]', pp).forEach((c) => (c.onclick = () => {
      const id = c.dataset.cam; let l = p.cameras ? [...p.cameras] : [];
      l = l.includes(id) ? l.filter((x) => x !== id) : [...l, id]; p.cameras = l.length ? l : null; save(); render();
    }));
    H.$$('[data-g]', pp).forEach((c) => (c.onclick = () => { const g = c.dataset.g; p.groups = p.groups.includes(g) ? p.groups.filter((x) => x !== g) : [...p.groups, g]; save(); render(); }));
    H.$$('[data-f]', pp).forEach((b) => (b.onclick = () => { p.faces = b.dataset.f; save(); render(); }));
    const cd = H.$('[data-cd]', pp); cd.oninput = () => { cd.nextElementSibling.textContent = `${cd.value} s`; }; cd.onchange = () => { p.cooldown = +cd.value; save(); };
    H.$$('[data-q]', pp).forEach((i) => (i.onchange = () => { p[i.dataset.q] = i.value; save(); }));
  };
  render();
}

function renderInstall(box) {
  const draw = () => {
    if (isStandalone()) { box.innerHTML = '<div class="storage-status ok">✓ CamSpire läuft als installierte App.</div>'; return; }
    if (installPrompt) {
      box.innerHTML = '<p class="muted" style="margin-top:0">Als App auf dem Startbildschirm/Desktop – eigenes Fenster, schneller Start, Benachrichtigungen.</p><button class="btn primary" id="i-go">App installieren</button>';
      H.$('#i-go', box).onclick = async () => { installPrompt.prompt(); const r = await installPrompt.userChoice; installPrompt = null; if (r.outcome === 'accepted') H.toast('CamSpire wird installiert'); draw(); };
      return;
    }
    if (isIOS()) {
      box.innerHTML = `<ol class="steps"><li>In Safari unten auf <b>Teilen</b> <span class="muted">(Quadrat mit Pfeil)</span> tippen</li><li><b>Zum Home-Bildschirm</b> wählen</li><li>CamSpire vom Home-Bildschirm öffnen – dann hier Benachrichtigungen aktivieren</li></ol>`;
      return;
    }
    box.innerHTML = !window.isSecureContext
      ? '<div class="storage-status bad">Installation braucht HTTPS – öffne CamSpire über deine Domain.</div>'
      : '<p class="muted" style="margin:0">Im Browser-Menü <b>„App installieren“</b> bzw. <b>„Zum Startbildschirm hinzufügen“</b> wählen. (Falls der Knopf hier nicht erscheint, ist die App evtl. schon installiert.)</p>';
  };
  draw();
  document.addEventListener('camspire-installable', draw, { once: true });
}

// ---------------------------------------------------------------- Benutzerverwaltung (Admin)
export async function usersTab(el) {
  const data = await H.api('/api/users');
  const camName = (id) => data.cameras.find((c) => c.id === id)?.name || id;
  el.innerHTML = `
    <div class="row-actions"><button class="btn primary" id="u-add">${H.ICON.plus} Benutzer anlegen</button></div>
    <div class="cam-list">${data.users.map((u) => `
      <div class="cam-row user-row ${u.disabled ? 'off' : ''}" data-id="${u.id}">
        <div class="avatar-s">${H.esc((u.display_name || u.username).slice(0, 1).toUpperCase())}</div>
        <div class="info"><div class="n">${H.esc(u.display_name || u.username)} <span class="pill ${u.role === 'admin' ? 'ok' : ''}">${u.role === 'admin' ? 'Admin' : 'Benutzer'}</span>${u.disabled ? ' <span class="pill bad">gesperrt</span>' : ''}</div>
          <div class="m">@${H.esc(u.username)} · ${u.role === 'admin' ? 'alle Rechte' : u.perms.map((p) => data.perms[p]?.split(' (')[0]).join(', ') || 'keine Rechte'}</div>
          <div class="m">${u.role === 'admin' || u.cameras === null ? 'Alle Kameras' : u.cameras.map(camName).join(', ') || 'keine Kamera'} · ${u.passkeys} Passkey(s) · zuletzt ${fmtDate(u.last_login)}</div></div>
        <button class="btn small">Bearbeiten</button></div>`).join('')}</div>`;
  H.$('#u-add', el).onclick = () => editUser(null, data, () => usersTab(el));
  H.$$('.user-row', el).forEach((r) => (r.onclick = () => editUser(data.users.find((u) => u.id === +r.dataset.id), data, () => usersTab(el))));
}

function editUser(u, data, done) {
  const isNew = !u;
  const st = u ? JSON.parse(JSON.stringify(u)) : { username: '', display_name: '', role: 'user', perms: ['live', 'history', 'events', 'download'], cameras: null, disabled: false };
  const card = H.openModal(`
    <div class="m-head"><h2>${isNew ? 'Neuer Benutzer' : H.esc(st.display_name || st.username)}</h2><span class="spacer"></span>
      ${isNew ? '' : `<button class="btn icon danger" id="u-del" title="Löschen">${H.ICON.trash}</button>`}<button class="btn icon" data-close>${H.ICON.close}</button></div>
    <div class="m-body cam-editor">
      <section>
        <div class="f-row"><div class="f-label">Benutzername</div><div class="f-ctl"><input type="text" id="u-name" value="${H.esc(st.username)}" ${isNew ? '' : 'disabled'} autocapitalize="none"></div></div>
        <div class="f-row"><div class="f-label">Anzeigename</div><div class="f-ctl"><input type="text" id="u-disp" value="${H.esc(st.display_name || '')}"></div></div>
        <div class="f-row"><div class="f-label">${isNew ? 'Passwort' : 'Neues Passwort'}<small>${isNew ? 'mind. 8 Zeichen' : 'leer lassen = unverändert'}</small></div><div class="f-ctl"><input type="password" id="u-pw" autocomplete="new-password"></div></div>
        <div class="f-row"><div class="f-label">Rolle</div><div class="f-ctl"><div class="seg" id="u-role"><button data-v="user">Benutzer</button><button data-v="admin">Admin</button></div></div></div>
        ${isNew ? '' : '<div class="f-row"><div class="f-label">Gesperrt</div><div class="f-ctl"><button class="switch" id="u-dis"></button></div></div>'}
      </section>
      <section id="u-rights"><h3>Rechte</h3>
        <div class="chips" style="margin-bottom:10px">${Object.keys(PRESETS).map((k) => `<span class="chip" data-pre="${k}">Vorlage: ${k}</span>`).join('')}</div>
        <div class="perm-list">${Object.entries(data.perms).map(([k, l]) => `<label class="perm"><input type="checkbox" value="${k}" ${st.perms.includes(k) ? 'checked' : ''}> ${H.esc(l)}</label>`).join('')}</div>
        <h3 style="margin-top:16px">Kameras</h3>
        <label class="perm"><input type="checkbox" id="u-allcams" ${st.cameras === null ? 'checked' : ''}> Alle Kameras (auch neue)</label>
        <div class="chips" id="u-cams">${data.cameras.map((c) => `<span class="chip ${st.cameras && st.cameras.includes(c.id) ? 'on' : ''}" data-c="${c.id}">${H.esc(c.name)}</span>`).join('')}</div>
      </section>
      ${isNew ? '' : '<section><h3>Passkeys</h3><div id="u-pks"></div></section>'}
      <div class="m-foot"><span class="spacer"></span><button class="btn" data-close>Abbrechen</button><button class="btn primary" id="u-save">Speichern</button></div>
    </div>`);
  const syncRole = () => {
    H.$$('#u-role button', card).forEach((b) => b.classList.toggle('on', b.dataset.v === st.role));
    H.$('#u-rights', card).hidden = st.role === 'admin';
  };
  H.$$('#u-role button', card).forEach((b) => (b.onclick = () => { st.role = b.dataset.v; syncRole(); }));
  syncRole();
  const dis = H.$('#u-dis', card);
  if (dis) { dis.classList.toggle('on', st.disabled); dis.onclick = () => { st.disabled = !st.disabled; dis.classList.toggle('on', st.disabled); }; }
  const camsBox = H.$('#u-cams', card), all = H.$('#u-allcams', card);
  const syncCams = () => { camsBox.style.opacity = all.checked ? 0.4 : 1; camsBox.style.pointerEvents = all.checked ? 'none' : ''; };
  all.onchange = syncCams; syncCams();
  H.$$('[data-c]', camsBox).forEach((c) => (c.onclick = () => c.classList.toggle('on')));
  H.$$('[data-pre]', card).forEach((c) => (c.onclick = () => { const set = PRESETS[c.dataset.pre]; H.$$('.perm-list input', card).forEach((i) => (i.checked = set.includes(i.value))); }));
  if (!isNew) {
    (async () => {
      const pks = await H.api(`/api/users/${u.id}/passkeys`);
      H.$('#u-pks', card).innerHTML = pks.length ? `<div class="list">${pks.map((p) => `<div class="li"><div><b>${H.esc(p.name)}</b><small>${fmtDate(p.created)}</small></div><button class="btn icon small danger" data-p="${p.id}">${H.ICON.trash}</button></div>`).join('')}</div>` : '<p class="muted">Keine Passkeys</p>';
      H.$$('[data-p]', card).forEach((b) => (b.onclick = async () => { await H.api(`/api/users/${u.id}/passkeys/${b.dataset.p}`, { method: 'DELETE' }); b.closest('.li').remove(); }));
    })();
    H.$('#u-del', card).onclick = async () => {
      if (!confirm(`Benutzer „${st.username}“ löschen?`)) return;
      try { await H.api(`/api/users/${u.id}`, { method: 'DELETE' }); H.closeModal(); done(); } catch (e) { H.toast(`<span class="bad">${H.esc(e.message)}</span>`); }
    };
  }
  H.$('#u-save', card).onclick = async () => {
    const body = { display_name: H.$('#u-disp', card).value, role: st.role,
      perms: H.$$('.perm-list input:checked', card).map((i) => i.value),
      all_cameras: all.checked, cameras: all.checked ? null : H.$$('[data-c].on', camsBox).map((c) => c.dataset.c) };
    const pw = H.$('#u-pw', card).value;
    if (pw) body.password = pw;
    if (!isNew) body.disabled = st.disabled;
    try {
      if (isNew) { body.username = H.$('#u-name', card).value.trim(); await H.api('/api/users', { method: 'POST', json: body }); }
      else await H.api(`/api/users/${u.id}`, { method: 'PATCH', json: body });
      H.closeModal(); H.toast('Gespeichert'); done();
    } catch (e) { H.toast(`<span class="bad">${H.esc(e.message)}</span>`); }
  };
}
