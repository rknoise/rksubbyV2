/* rksubby lejátszó: a capture kártya képe (böngésző), rajta a felirat; a hang a programból szól.
   Az ablakot a program tartja mindig legfelül (a lejátszó időnként jelentkezik: /api/player/hello). */
'use strict';

const $ = (id) => document.getElementById(id);
const P = { conf: null, stream: null, label: '', err: '', labels: [], trace: [], status: null, idleTimer: null, retryTimer: null };

function trace(msg) {   // a lépések a programhoz is eljutnak (hibakereséshez)
  P.trace.push(new Date().toLocaleTimeString('hu-HU') + ' ' + msg);
  P.trace = P.trace.slice(-12);
}
addEventListener('error', (e) => trace('JS-hiba: ' + e.message));
addEventListener('unhandledrejection', (e) => trace('JS-hiba: ' + (e.reason && e.reason.message || e.reason)));
const CAPTURE_RE = /avermedia|live gamer|gc5\d\d|capture|hdmi|elgato|cam link|usb video|game|magewell|razer ripsaw/i;

async function api(path, body) {
  const opt = body === undefined ? { cache: 'no-store' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch('/api/' + path, opt);
  let j = {};
  try { j = await r.json(); } catch (e) { /* nincs JSON */ }
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

// ------------------------------------------------------------------ kép

function showMsg(title, text, button = true) {
  $('msg-title').textContent = title;
  $('msg-text').textContent = text;
  $('msg-btn').hidden = !button;
  $('msg').hidden = false;
}

function stopStream() {
  if (P.stream) P.stream.getTracks().forEach((t) => t.stop());
  P.stream = null;
}

async function videoInputs() {
  let devs = await navigator.mediaDevices.enumerateDevices();
  trace(`képforrások: ${devs.filter((d) => d.kind === 'videoinput').length} db, név ${devs.some((d) => d.label) ? 'van' : 'nincs'}`);
  if (!devs.some((d) => d.kind === 'videoinput' && d.label)) {
    // a nevek csak engedély után, és csak amíg egy képforrás nyitva van, látszanak:
    // egy pillanatra megnyitjuk az alapértelmezettet, és közben kérdezzük le a listát
    const tmp = await navigator.mediaDevices.getUserMedia({ video: true });
    trace('engedély megvan');
    try {
      devs = await navigator.mediaDevices.enumerateDevices();
      trace(`képforrások engedéllyel: ${devs.filter((d) => d.kind === 'videoinput').map((d) => d.label).join(' | ')}`);
    } finally {
      tmp.getTracks().forEach((t) => t.stop());
    }
  }
  return devs.filter((d) => d.kind === 'videoinput');
}

async function openVideo() {
  clearTimeout(P.retryTimer);
  stopStream();
  const c = P.conf.config;
  let vids;
  try {
    vids = await videoInputs();
    P.labels = vids.map((d) => d.label);
  } catch (e) {
    trace('engedély/lista hiba: ' + e.name + ': ' + e.message);
    showMsg('Nincs engedély a képforráshoz', 'Engedélyezd a kamerát/képforrást ennek az ablaknak (a címsor melletti ikonnal), majd töltsd újra (F5).');
    report(e.message);
    return;
  }
  const want = c.video_device || '';
  const named = vids.filter((d) => d.label && d.deviceId);
  const dev = (want && (named.find((d) => d.label === want) || named.find((d) => d.label.includes(want) || want.includes(d.label))))
    || (!want && named.find((d) => CAPTURE_RE.test(d.label)));
  if (!dev) {
    showMsg(want ? 'Nem találom a képforrást' : 'Nincs kiválasztva képforrás',
      want ? `„${want}” most nem érhető el. Be van kapcsolva és csatlakoztatva a kártya?` : 'Válaszd ki a capture kártyát a beállításoknál (2. lépés: Forrás).');
    report('nincs képforrás: ' + want);
    P.retryTimer = setTimeout(openVideo, 4000);
    return;
  }
  const [w, h] = (c.video_res || '1920x1080').split('x').map(Number);
  try {
    P.stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { deviceId: { exact: dev.deviceId }, width: { ideal: w }, height: { ideal: h }, frameRate: { ideal: c.video_fps || 60 } },
    });
  } catch (e) {
    trace('megnyitási hiba: ' + e.name + ': ' + e.message);
    showMsg('A képforrás nem nyitható meg', `${dev.label}: ${e.message}. Ha egy másik program (pl. a kártya saját programja) kizárólagosan használja, zárd be.`);
    report(e.message);
    P.retryTimer = setTimeout(openVideo, 5000);
    return;
  }
  P.label = dev.label;
  trace('megnyitva: ' + dev.label);
  const track = P.stream.getVideoTracks()[0];
  track.addEventListener('ended', () => {
    showMsg('Megszakadt a kép', 'A képforrás leállt. Újrapróbálom…', false);
    P.retryTimer = setTimeout(openVideo, 3000);
  });
  $('video').srcObject = P.stream;
  $('msg').hidden = true;
  report('');
}

function report(error) {
  if (error !== undefined) P.err = error;
  const t = P.stream && P.stream.getVideoTracks()[0];
  const s = t ? t.getSettings() : {};
  api('player/hello', { video: P.label, res: s.width ? `${s.width}x${s.height}@${Math.round(s.frameRate || 0)}` : '',
    error: P.err, devices: P.labels, trace: P.trace }).catch(() => {});
}

// ------------------------------------------------------------------ elrendezés: a felirat a kép fölött

function layout() {
  const v = $('video');
  const W = innerWidth;
  const H = innerHeight;
  const vw = v.videoWidth || 16;
  const vh = v.videoHeight || 9;
  const s = Math.min(W / vw, H / vh);
  const w = vw * s;
  const h = vh * s;
  const st = $('stage').style;
  st.left = (W - w) / 2 + 'px';
  st.top = (H - h) / 2 + 'px';
  st.width = w + 'px';
  st.height = h + 'px';
  document.documentElement.style.setProperty('--k', String(h / 1080));
}

// ------------------------------------------------------------------ vezérlősáv

function setOn(btn, on) { btn.classList.toggle('on', !!on); }

async function setCfg(changes) {
  try {
    const j = await api('config', changes);
    P.conf.config = j.config;
    renderBar();
    if ('player_topmost' in changes) report();
  } catch (e) { /* a következő lekérdezés helyrehozza */ }
}

function renderBar() {
  const c = P.conf.config;
  setOn($('b-mute'), c.monitor_audio);
  $('wave').style.display = c.monitor_audio ? '' : 'none';
  if (document.activeElement !== $('b-vol')) $('b-vol').value = c.monitor_volume;
  setOn($('b-top'), c.player_topmost);
  setOn($('b-cc'), P.status ? !P.status.paused : true);
  const state = P.status ? P.status.engine.state : 'idle';
  const run = $('b-run');
  run.className = state === 'running' || state === 'loading' ? 'stop' : 'primary';
  run.textContent = { running: 'Leállítás', loading: 'Indul…', stopping: 'Leáll…' }[state] || 'Indítás';
  run.disabled = state === 'stopping';
}

function renderStatus(st) {
  P.status = st;
  const e = st.engine;
  const dot = $('chip-dot');
  let text = { idle: 'A feliratozás áll', loading: 'Indul…', running: 'Feliratozás fut', stopping: 'Leáll…', error: 'Hiba' }[e.state];
  dot.className = 'dot ' + ({ running: 'run', loading: 'load', stopping: 'load', error: 'err' }[e.state] || '');
  if (e.state === 'loading' && e.steps.length) text = e.steps[e.steps.length - 1].msg;
  if (e.state === 'error') text = 'Hiba: ' + e.error;
  if (e.state === 'running' && e.audio && (e.audio.error || e.audio.stalled)) {
    dot.className = 'dot err';
    text = e.audio.error || 'Nem jön hang a forrásból';
  }
  if (st.paused) text += ' · felirat kikapcsolva';
  $('chip-text').textContent = text;
  if (e.state === 'error' || e.state === 'loading' || e.state === 'stopping') wake();
  renderBar();
}

async function poll() {
  try {
    renderStatus(await api('status'));
  } catch (e) {
    $('chip-dot').className = 'dot err';
    $('chip-text').textContent = 'Nem érem el a programot';
  }
  setTimeout(poll, P.status && P.status.engine.state === 'loading' ? 500 : 1000);
}

async function toggleRun() {
  const state = P.status ? P.status.engine.state : 'idle';
  try {
    if (state === 'running' || state === 'loading') await api('engine/stop', {});
    else if (state !== 'stopping') await api('engine/start', { mode: 'live' });
  } catch (e) {
    $('chip-text').textContent = e.message;
  }
}

function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => {});
}

function wake() {
  document.body.classList.remove('idle');
  clearTimeout(P.idleTimer);
  P.idleTimer = setTimeout(() => {
    if (!$('bar').matches(':hover')) document.body.classList.add('idle');
  }, 2500);
}

function bind() {
  $('b-run').addEventListener('click', toggleRun);
  $('b-mute').addEventListener('click', () => setCfg({ monitor_audio: !P.conf.config.monitor_audio }));
  $('b-vol').addEventListener('input', () => {
    clearTimeout(P.volTimer);
    P.volTimer = setTimeout(() => setCfg({ monitor_volume: Number($('b-vol').value), monitor_audio: true }), 120);
  });
  $('b-cc').addEventListener('click', () => api('subs/pause', { paused: !(P.status && P.status.paused) }).then(poll1));
  $('b-top').addEventListener('click', () => setCfg({ player_topmost: !P.conf.config.player_topmost }));
  $('b-fs').addEventListener('click', toggleFullscreen);
  $('b-set').addEventListener('click', () => api('app/open', { view: 'dash' }).catch(() => {}));
  $('msg-btn').addEventListener('click', () => api('app/open', { view: 's2' }).catch(() => {}));
  $('video').addEventListener('dblclick', toggleFullscreen);
  $('video').addEventListener('loadedmetadata', layout);
  $('video').addEventListener('resize', layout);
  addEventListener('resize', layout);
  addEventListener('mousemove', wake);
  addEventListener('keydown', (ev) => {
    if (ev.target.tagName === 'INPUT' && ev.key !== 'Escape') return;
    const k = ev.key.toLowerCase();
    if (k === 'f' || k === 'enter') toggleFullscreen();
    else if (k === 'm') $('b-mute').click();
    else if (k === 's') $('b-cc').click();
    else if (k === 't') $('b-top').click();
    else if (k === ' ') { ev.preventDefault(); toggleRun(); }
    else return;
    wake();
  });
}

async function poll1() {
  try { renderStatus(await api('status')); } catch (e) { /* majd a következő */ }
}

async function init() {
  bind();
  RkSubs.mount($('sub'));
  try {
    P.conf = await api('config');
  } catch (e) {
    showMsg('Nem érem el a programot', 'Fut még az rksubby? Indítsd el újra.', false);
    return;
  }
  renderBar();
  layout();
  wake();
  poll();
  await openVideo();
  setInterval(() => report(), 4000);   // jelentkezés: a program ilyenkor (újra) legfelülre teszi az ablakot
  navigator.mediaDevices.addEventListener('devicechange', () => { if (!P.stream) openVideo(); });
}

init();
