// CamSpire NVR – Verlauf: Wiedergabe mit vertikaler Zeitachse (oben = jetzt, unten = älter)
let H;
const GCOL = { person: '#ffc850', vehicle: '#3caaff', animal: '#78e678' };
const LABEL = { person: 'Person', car: 'Auto', truck: 'LKW', bus: 'Bus', motorcycle: 'Motorrad', bicycle: 'Fahrrad',
  dog: 'Hund', cat: 'Katze', bird: 'Vogel', horse: 'Pferd' };
const SPEEDS = [1, 2, 4, 8, 16];
const pad = (n) => String(n).padStart(2, '0');
const hms = (t) => { const d = new Date(t * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
const hm = (t) => { const d = new Date(t * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const dayLabel = (t) => new Date(t * 1000).toLocaleDateString('de-DE', { weekday: 'short', day: '2-digit', month: '2-digit' });
const now = () => Date.now() / 1000;

let HlsMod = null;
async function loadHls() {
  if (HlsMod !== null) return HlsMod;
  await new Promise((res) => {
    const s = document.createElement('script'); s.src = '/static/vendor/hls.light.min.js';
    s.onload = res; s.onerror = res; document.head.append(s);
  });
  HlsMod = window.Hls || false;
  return HlsMod;
}

// Playlist selbst lesen: Zuordnung Wanduhrzeit <-> Position im Video
function parsePlaylist(text) {
  const frags = []; let pdt = null, dur = 0, pos = 0;
  for (const line of text.split('\n')) {
    if (line.startsWith('#EXT-X-PROGRAM-DATE-TIME:')) pdt = Date.parse(line.slice(25)) / 1000;
    else if (line.startsWith('#EXTINF:')) dur = parseFloat(line.slice(8));
    else if (line && !line.startsWith('#')) { frags.push({ t: pdt, d: dur, m: pos }); pos += dur; }
  }
  return frags;
}

export async function historyPage(helpers, params = {}) {
  H = helpers;
  const cams = H.state.cams.filter((c) => c.record);
  const all = cams.length ? cams : H.state.cams;
  if (!all.length) { H.main.innerHTML = `<div class="page-head"><h1>Verlauf</h1></div><div class="empty">${H.ICON.empty}Noch keine Kamera eingerichtet.</div>`; return; }
  const S = {
    cam: all.find((c) => c.id === params.cam)?.id || all[0].id,
    t: params.t ? Number(params.t) : now() - 60,
    spp: 3,                 // Sekunden pro Pixel (Zoom): 15 min ≈ 300 px
    ranges: [], events: [], win: [0, 0], first: null, pre: 5,
    playing: false, live: !params.t, speed: 1,
    hls: null, frags: [], pl: [0, 0], loading: false, seq: 0, dragging: false, liveTimer: null, raf: 0,
  };

  H.main.innerHTML = `
    <div class="page-head"><h1>Verlauf</h1>
      <div class="chips" id="h-cams">${all.map((c) => `<span class="chip ${c.id === S.cam ? 'on' : ''}" data-c="${c.id}">${H.esc(c.name)}</span>`).join('')}</div>
      <span class="spacer"></span><input type="date" id="h-date" class="datein"></div>
    <div class="hist">
      <div class="hist-main">
        <div class="hist-player">
          <video id="h-video" playsinline muted></video>
          <div class="hist-live" id="h-live"></div>
          <div class="hist-clock" id="h-clock"></div>
          <div class="hist-gap" id="h-gap" hidden></div>
          <div class="hist-badge" id="h-badge"></div>
        </div>
        <div class="hist-ctl">
          <button class="btn icon" id="h-prev" title="Vorheriges Ereignis">⏮</button>
          <button class="btn icon" id="h-back" title="10 s zurück">−10s</button>
          <button class="btn primary icon play" id="h-play" title="Abspielen/Pause">▶</button>
          <button class="btn icon" id="h-fwd" title="10 s vor">+10s</button>
          <button class="btn icon" id="h-next" title="Nächstes Ereignis">⏭</button>
          <div class="seg" id="h-speed">${SPEEDS.map((s) => `<button data-s="${s}" class="${s === 1 ? 'on' : ''}">${s}×</button>`).join('')}</div>
          <span class="spacer"></span>
          <button class="btn small" id="h-dl" title="Ausschnitt als MP4 herunterladen">⬇ Ausschnitt</button>
          <button class="btn small live-btn" id="h-golive">● Live</button>
        </div>
        <div class="hist-evs" id="h-evs"></div>
      </div>
      <div class="hist-tl" id="h-tl">
        <canvas id="h-canvas"></canvas>
        <div class="tl-zoom"><button class="btn icon small" id="h-zin" title="Hineinzoomen">+</button><button class="btn icon small" id="h-zout" title="Herauszoomen">−</button></div>
      </div>
    </div>`;

  const video = H.$('#h-video'), cv = H.$('#h-canvas'), ctx = cv.getContext('2d'), tl = H.$('#h-tl');
  const gap = H.$('#h-gap'), clock = H.$('#h-clock'), liveBox = H.$('#h-live'), badge = H.$('#h-badge');
  const camObj = () => H.state.cams.find((c) => c.id === S.cam);
  let seekTimer = 0;

  // ---------------------------------------------------------------- Daten
  const viewSpan = () => tl.clientHeight * S.spp;
  async function ensureData(force = false) {
    const span = Math.max(viewSpan() * 2, 6 * 3600);
    if (!force && S.t - span / 2 > S.win[0] && S.t + span / 4 < S.win[1]) return;
    const a = S.t - span, b = Math.min(now() + 60, S.t + span);
    const r = await H.api(`/api/timeline?camera=${S.cam}&start=${a}&end=${b}`);
    S.ranges = r.ranges; S.events = r.events; S.first = r.first; S.pre = r.pre_roll; S.win = [a, b];
    renderEventStrip(); draw();
  }
  const rangeAt = (t) => S.ranges.find(([a, b]) => t >= a - 0.5 && t < b);
  const nextRange = (t) => S.ranges.find(([a]) => a > t);

  // ---------------------------------------------------------------- Zeichnen
  // Abspielposition: bei „jetzt“ ganz oben (wie gewohnt), beim Zurückblättern wandert sie bis 35 % nach unten
  const playY = (h) => Math.round(Math.min(h * 0.35, Math.max(26, (now() - S.t) / S.spp + 26)));
  function draw() {
    cancelAnimationFrame(S.raf);
    S.raf = requestAnimationFrame(() => {
      const w = tl.clientWidth, h = tl.clientHeight, dpr = devicePixelRatio || 1;
      if (cv.width !== w * dpr || cv.height !== h * dpr) { cv.width = w * dpr; cv.height = h * dpr; cv.style.width = w + 'px'; cv.style.height = h + 'px'; }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      const py = playY(h), ax = Math.round(w * 0.58);
      const yOf = (t) => py + (S.t - t) / S.spp;
      const tTop = S.t + py * S.spp, tBot = S.t - (h - py) * S.spp;
      // Zukunft abdunkeln
      const yNow = yOf(now());
      if (yNow > 0) { ctx.fillStyle = 'rgba(255,255,255,.025)'; ctx.fillRect(0, 0, w, Math.min(h, yNow)); }
      // Raster
      const minPx = 60 / S.spp;
      const minor = minPx >= 6 ? 60 : minPx >= 1.2 ? 300 : 900;
      const major = minPx >= 6 ? 900 : minPx >= 2 ? 1800 : minPx >= 0.5 ? 3600 : 3 * 3600;
      const tz = new Date().getTimezoneOffset() * 60;
      ctx.font = '11px Inter, system-ui, sans-serif'; ctx.textBaseline = 'middle';
      for (let t = Math.floor((tBot - tz) / minor) * minor + tz; t <= tTop; t += minor) {
        const y = yOf(t); const isMaj = ((t - tz) % major + major) % major === 0;
        const isMid = ((t - tz) % (minor * 5) + minor * 5) % (minor * 5) === 0;
        ctx.fillStyle = isMaj ? '#5b6788' : '#33405f';
        ctx.fillRect(0, y, isMaj ? 18 : isMid ? 12 : 7, 1);
        if (isMaj) {
          const d = new Date(t * 1000);
          ctx.fillStyle = '#8a94b0';
          ctx.fillText(d.getHours() === 0 && d.getMinutes() === 0 ? dayLabel(t) : hm(t), 22, y);
        }
      }
      // Achse
      ctx.fillStyle = '#26314e'; ctx.fillRect(ax - 1, 0, 2, h);
      // Aufnahmen
      for (const [a, b, ev] of S.ranges) {
        const y1 = yOf(b), y2 = yOf(a); if (y2 < 0 || y1 > h) continue;
        ctx.fillStyle = ev ? 'rgba(94,234,212,.75)' : 'rgba(120,140,200,.55)';
        ctx.fillRect(ax - 3, y1, 6, Math.max(1, y2 - y1));
      }
      // Ereignisse
      S.events.forEach((e) => {
        const end = e.end_ts || now(); const y1 = yOf(end), y2 = yOf(e.start_ts);
        if (y2 < -10 || y1 > h + 10) return;
        const c = GCOL[e.grp] || '#ccc';
        ctx.fillStyle = c; ctx.globalAlpha = .9;
        ctx.fillRect(ax + 6, y1, 4, Math.max(2, y2 - y1));
        ctx.fillRect(ax - 14, y2 - 1.5, 28, 3);                   // Marker am Beginn
        ctx.globalAlpha = 1;
        if (minPx >= 4 && (y2 - y1) > -1) {
          ctx.fillStyle = c; ctx.font = '600 10px Inter, system-ui, sans-serif';
          const txt = e.sub_label || LABEL[e.label] || e.label;
          ctx.fillText(txt, ax + 14, y2 - 6);
        }
      });
      // Jetzt-Linie
      if (yNow >= 0 && yNow <= h) { ctx.setLineDash([3, 3]); ctx.strokeStyle = '#f87171aa'; ctx.beginPath(); ctx.moveTo(0, yNow); ctx.lineTo(w, yNow); ctx.stroke(); ctx.setLineDash([]); }
      // Abspielposition
      ctx.fillStyle = '#ef4444'; ctx.fillRect(0, py - 1, w, 2);
      const label = hms(S.t);
      ctx.font = '700 12px Inter, system-ui, sans-serif';
      const tw = ctx.measureText(label).width + 18;
      const lx = Math.min(w - tw - 4, Math.max(4, ax - tw / 2));
      ctx.beginPath(); ctx.roundRect(lx, py - 11, tw, 22, 11); ctx.fill();
      ctx.fillStyle = '#fff'; ctx.fillText(label, lx + 9, py + 1);
      clock.textContent = `${new Date(S.t * 1000).toLocaleDateString('de-DE')} ${hms(S.t)}`;
    });
  }

  // ---------------------------------------------------------------- Wiedergabe
  function mediaFor(t) {
    const f = S.frags.find((x) => t >= x.t && t < x.t + x.d + 0.01);
    return f ? f.m + (t - f.t) : null;
  }
  function timeFor(m) {
    for (let i = S.frags.length - 1; i >= 0; i--) { const f = S.frags[i]; if (m >= f.m) return f.t + Math.min(f.d, m - f.m); }
    return null;
  }
  function stopLive() {
    S.live = false; clearInterval(S.liveTimer); if (S.tile) { S.tile.destroy(); S.tile = null; } liveBox.innerHTML = ''; liveBox.hidden = true; video.hidden = false;
    H.$('#h-golive').classList.remove('on');
  }
  async function goLive() {
    S.live = true; S.playing = false; destroyHls(); video.pause(); video.hidden = true; gap.hidden = true;
    H.$('#h-golive').classList.add('on'); setPlayIcon();
    const cam = camObj(); liveBox.hidden = false; liveBox.innerHTML = '';
    if (S.tile) S.tile.destroy();
    S.tile = H.camTile(cam, { big: true }); liveBox.append(S.tile);
    clearInterval(S.liveTimer);
    S.liveTimer = setInterval(() => { S.t = now(); draw(); }, 1000);
    S.t = now(); badge.textContent = 'LIVE'; badge.className = 'hist-badge live'; await ensureData(true); draw();
  }
  function destroyHls() { if (S.hls) { S.hls.destroy(); S.hls = null; } S.frags = []; S.pl = [0, 0]; video.removeAttribute('src'); }

  async function seek(t, play = S.playing) {
    clearTimeout(seekTimer);
    const token = ++S.seq;                       // nur die letzte Anfrage zählt
    if (S.live) stopLive();
    S.t = Math.min(t, now()); draw();
    await ensureData();
    if (token !== S.seq) return;
    const r = rangeAt(S.t);
    if (!r) {
      video.pause(); video.hidden = true;
      const n = nextRange(S.t);
      gap.hidden = false;
      gap.innerHTML = `<div>Keine Aufnahme um ${hms(S.t)}</div>${n ? `<button class="btn small primary" id="h-jump">Nächste Aufnahme ${hm(n[0])} ▶</button>` : ''}`;
      const j = H.$('#h-jump'); if (j) j.onclick = () => seek(n[0], true);
      badge.textContent = ''; badge.className = 'hist-badge';
      return;
    }
    gap.hidden = true; video.hidden = false;
    let m = mediaFor(S.t);
    if (m === null) {
      await loadPlaylist(Math.max(r[0], S.t - 2));
      if (token !== S.seq) return;
      m = mediaFor(S.t);
    }
    if (m !== null) { try { video.currentTime = m; } catch {} }
    badge.textContent = r[2] ? 'Ereignis' : 'Daueraufnahme'; badge.className = 'hist-badge';
    if (play) { video.playbackRate = S.speed; video.play().catch(() => {}); S.playing = true; } else video.pause();
    setPlayIcon();
  }

  async function loadPlaylist(start) {
    S.loading = true;
    const end = Math.min(now(), start + 3600);
    const url = `/api/cameras/${S.cam}/vod.m3u8?start=${start.toFixed(1)}&end=${end.toFixed(1)}`;
    let text;
    try { text = await (await fetch(url)).text(); } catch { S.loading = false; return; }
    S.frags = parsePlaylist(text);
    if (!S.frags.length) { S.loading = false; return; }
    const last = S.frags[S.frags.length - 1];
    S.pl = [S.frags[0].t, last.t + last.d];
    if (S.hls) { S.hls.destroy(); S.hls = null; }
    const Hls = await loadHls();
    await new Promise((resolve) => {
      if (Hls && Hls.isSupported()) {
        S.hls = new Hls({ maxBufferLength: 20, backBufferLength: 30, enableWorker: true, startPosition: -1 });
        S.hls.on(Hls.Events.MANIFEST_PARSED, resolve);
        S.hls.on(Hls.Events.ERROR, (_, d) => { if (d.fatal) { console.warn('hls', d); resolve(); } });
        S.hls.loadSource(url); S.hls.attachMedia(video);
      } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = url; video.addEventListener('loadedmetadata', resolve, { once: true });
      } else resolve();
      setTimeout(resolve, 8000);
    });
    S.loading = false;
  }

  video.addEventListener('timeupdate', async () => {
    if (S.live || S.dragging || S.loading) return;
    const t = timeFor(video.currentTime);
    if (t === null) return;
    S.t = t; draw();
    if (S.playing && S.pl[1] - t < 20 && S.pl[1] < now() - 5) {
      const n = S.ranges.find(([a, b]) => b > S.pl[1] + 0.5);
      if (n && !S.loading) { const keep = S.t; await loadPlaylist(Math.max(n[0], S.pl[1] - 4)); await seek(Math.max(keep, n[0]), true); }
    }
  });
  video.addEventListener('ended', async () => {
    if (S.live) return;
    const n = nextRange(S.pl[1] - 0.5);
    if (n) await seek(n[0], true); else { S.playing = false; setPlayIcon(); }
  });
  const setPlayIcon = () => { H.$('#h-play').textContent = S.playing && !S.live ? '⏸' : '▶'; };

  // ---------------------------------------------------------------- Bedienung Zeitachse
  let drag = null;
  const scheduleSeek = () => { clearTimeout(seekTimer); seekTimer = setTimeout(() => seek(S.t), 280); };
  cv.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (e.ctrlKey || e.metaKey) { zoom(e.deltaY > 0 ? 1.25 : 0.8); return; }
    if (S.live) stopLive();
    S.t = Math.min(now(), S.t - e.deltaY * S.spp * 0.6); S.seq++; draw(); ensureData(); scheduleSeek();
  }, { passive: false });
  cv.addEventListener('pointerdown', (e) => { drag = { y: e.clientY, t: S.t, moved: false, wasPlaying: S.playing }; cv.setPointerCapture(e.pointerId); });
  cv.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const dy = e.clientY - drag.y;
    if (Math.abs(dy) > 3) { drag.moved = true; S.dragging = true; if (S.live) stopLive(); video.pause(); }
    if (drag.moved) { S.t = Math.min(now(), drag.t + dy * S.spp); draw(); ensureData(); }
  });
  cv.addEventListener('pointerup', (e) => {
    if (!drag) return;
    const d = drag; drag = null; S.dragging = false;
    if (d.moved) { seek(S.t, d.wasPlaying); return; }
    // Klick: auf Ereignis-Marker springen, sonst an die Stelle
    const r = cv.getBoundingClientRect(); const y = e.clientY - r.top; const py = playY(tl.clientHeight);
    const t = S.t + (py - y) * S.spp;
    const hit = S.events.find((ev) => Math.abs(t - ev.start_ts) < 8 * S.spp || (t >= ev.start_ts && t <= (ev.end_ts || now())));
    seek(hit ? hit.start_ts - S.pre : t, true);
  });
  function zoom(f) { S.spp = Math.min(240, Math.max(0.5, S.spp * f)); ensureData(); draw(); }
  H.$('#h-zin').onclick = () => zoom(0.66);
  H.$('#h-zout').onclick = () => zoom(1.5);
  new ResizeObserver(draw).observe(tl);

  // ---------------------------------------------------------------- Knöpfe
  H.$('#h-play').onclick = () => {
    if (S.live) { seek(now() - 30, true); return; }
    if (S.playing) { video.pause(); S.playing = false; } else { seek(S.t, true); return; }
    setPlayIcon();
  };
  H.$('#h-back').onclick = () => seek(S.t - 10);
  H.$('#h-fwd').onclick = () => seek(S.t + 10);
  H.$('#h-prev').onclick = () => { const e = [...S.events].reverse().find((x) => x.start_ts - S.pre < S.t - 3); if (e) seek(e.start_ts - S.pre, true); };
  H.$('#h-next').onclick = () => { const e = S.events.find((x) => x.start_ts - S.pre > S.t + 1); if (e) seek(e.start_ts - S.pre, true); else goLive(); };
  H.$$('#h-speed button').forEach((b) => (b.onclick = () => {
    S.speed = Number(b.dataset.s); video.playbackRate = S.speed;
    H.$$('#h-speed button').forEach((x) => x.classList.toggle('on', x === b));
  }));
  H.$('#h-golive').onclick = goLive;
  H.$('#h-dl').onclick = () => {
    const r = rangeAt(S.t); if (!r) return H.toast('An dieser Stelle gibt es keine Aufnahme');
    const a = Math.max(r[0], S.t - 30), b = Math.min(r[1] + 0, S.t + 90);
    location.href = `/api/cameras/${S.cam}/export.mp4?start=${a.toFixed(1)}&end=${b.toFixed(1)}`;
  };
  H.$$('#h-cams .chip').forEach((c) => (c.onclick = async () => {
    S.cam = c.dataset.c; H.$$('#h-cams .chip').forEach((x) => x.classList.toggle('on', x === c));
    destroyHls(); S.win = [0, 0];
    if (S.live) goLive(); else { await ensureData(true); seek(S.t); }
  }));
  const dateIn = H.$('#h-date');
  const setDate = () => { const d = new Date(S.t * 1000); dateIn.value = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
  setDate();
  dateIn.onchange = async () => {
    const [y, m, d] = dateIn.value.split('-').map(Number); if (!y) return;
    const end = Math.min(now(), new Date(y, m - 1, d, 23, 59, 59).getTime() / 1000);
    S.t = end; S.win = [0, 0]; await ensureData(true);
    const dayStart = new Date(y, m - 1, d).getTime() / 1000;
    const last = [...S.ranges].reverse().find(([a, b]) => a < end && b > dayStart);
    seek(last ? Math.min(last[1] - 5, end) : end);
  };

  function renderEventStrip() {
    const box = H.$('#h-evs');
    const evs = [...S.events].reverse().slice(0, 40);
    box.innerHTML = evs.length ? evs.map((e) => `<div class="hev" data-t="${e.start_ts}">
        ${e.thumb_url ? `<img src="${e.thumb_url}" loading="lazy" alt="">` : '<div class="noimg"></div>'}
        <div><b style="color:${GCOL[e.grp] || '#ccc'}">${H.esc(e.sub_label || LABEL[e.label] || e.label)}</b><span>${hms(e.start_ts)}</span></div></div>`).join('')
      : '<div class="muted" style="padding:6px 2px">Keine Ereignisse in diesem Zeitraum</div>';
    H.$$('.hev', box).forEach((el) => (el.onclick = () => seek(Number(el.dataset.t) - S.pre, true)));
  }

  // Start
  await ensureData(true);
  if (S.live) await goLive(); else await seek(S.t, true);
  const keys = (e) => {
    if (e.target.closest('input,textarea,select')) return;
    if (e.key === ' ') { e.preventDefault(); H.$('#h-play').click(); }
    if (e.key === 'ArrowLeft') seek(S.t - (e.shiftKey ? 60 : 5));
    if (e.key === 'ArrowRight') seek(S.t + (e.shiftKey ? 60 : 5));
  };
  document.addEventListener('keydown', keys);
  const refresh = setInterval(() => { if (S.t > now() - viewSpan()) ensureData(true); }, 15000);
  return () => { destroyHls(); clearInterval(S.liveTimer); clearInterval(refresh); document.removeEventListener('keydown', keys); if (S.tile) S.tile.destroy(); liveBox.innerHTML = ''; };
}
