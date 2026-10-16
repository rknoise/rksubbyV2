/* rksubby - beállító varázsló és vezérlőpult. Csak a program saját, helyi API-ját hívja (/api/...). */
'use strict';

const S = {
  conf: null,      // /api/config
  status: null,    // /api/status
  checks: null,    // /api/checks
  devices: null,   // /api/devices
  profile: null,
  view: null,
  online: true,
  quit: false,
  testFromN: null,
  histKey: '',
  installRunning: false,
};

const $ = (id) => document.getElementById(id);
const VIEWS = ['dash', 's1', 's2', 's3', 's4', 's5', 's6', 'adv'];
const STATE_TXT = { idle: 'Áll', loading: 'Indul…', running: 'Fut', stopping: 'Leáll…', error: 'Hiba' };
const TR_NAME = { deepl: 'DeepL', ollama: 'helyi AI (Ollama)', google: 'Google', google_web: 'Google (régi)', none: 'nincs fordítás', auto: 'DeepL, tartalék: Ollama' };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c !== null && c !== undefined && c !== false) el.append(c instanceof Node ? c : String(c));
  }
  return el;
}

async function api(path, body) {
  const opt = body === undefined ? { cache: 'no-store' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch('/api/' + path, opt);
  let j = {};
  try { j = await r.json(); } catch (e) { /* nincs JSON-válasz */ }
  if (!r.ok) throw new Error(j.error || j.message || ('HTTP ' + r.status));
  return j;
}

let toastTimer = null;
function toast(msg, kind = '', action = null) {
  const t = $('toast');
  t.className = 'toast ' + kind;
  t.replaceChildren(h('span', { text: msg }));
  if (action) {
    t.append(h('button', { class: 'btn small primary', type: 'button', onclick: () => { t.hidden = true; action.fn(); } }, action.label));
  }
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, action ? 10000 : 3500);
}

const fmtInt = (n) => Number(n || 0).toLocaleString('hu-HU');
function fmtMs(ms) {
  if (ms === null || ms === undefined || ms < 0) return '–';
  return ms >= 1000 ? (ms / 1000).toLocaleString('hu-HU', { maximumFractionDigits: 2 }) + ' mp' : Math.round(ms) + ' ms';
}
const cfgVal = (key) => (S.conf ? S.conf.config[key] : undefined);

// ------------------------------------------------------------------ nézetek

const enter = {
  s1: () => loadChecks(false),
  s2: () => { loadDevices().then(() => startMeter()); loadVideo(); },
  s3: () => { renderDeepl(); refreshUsage(); renderOllama(); },
  s4: () => { if (S.conf) renderProfiles(); },
  s5: () => fitPreview(),
};

function show(view) {
  if (!VIEWS.includes(view)) view = cfgVal('setup_done') ? 'dash' : 's1';
  const prev = S.view;
  S.view = view;
  for (const v of VIEWS) $('view-' + v).hidden = v !== view;
  document.querySelectorAll('.nav-item').forEach((a) => a.classList.toggle('active', a.dataset.view === view));
  if (location.hash !== '#' + view) history.replaceState(null, '', '#' + view);
  $('main').scrollTop = 0;
  if (prev === 's2' && view !== 's2') { stopMeter(); stopPreview(); }
  if (enter[view]) enter[view]();
  if (S.status) renderStatus(S.status);
}
window.addEventListener('hashchange', () => show(location.hash.slice(1)));

// ------------------------------------------------------------------ beállítások

async function loadConfig() {
  S.conf = await api('config');
  $('ver').textContent = 'v' + S.conf.version;
  $('s5-url').value = S.conf.obs_url;
  const p = S.conf.paths;
  $('adv-paths').textContent = `Beállítások: ${p.config} · Napló: ${p.log} · SRT-fájlok: ${p.srt_dir}`;
  fillCfg();
  renderDeepl();
  renderProfiles();
  updateDots();
}

function showOut(inp) {
  if (!inp.dataset.out) return;
  const v = Number(inp.value);
  $(inp.dataset.out).textContent = inp.dataset.pct ? Math.round(v * 100) + '%' : v + (inp.dataset.unit || '');
}

function fillCfg() {
  if (!S.conf) return;
  document.querySelectorAll('[data-cfg]').forEach((inp) => {
    const v = cfgVal(inp.dataset.cfg);
    if (inp.type === 'checkbox') inp.checked = !!v;
    else if (inp.type === 'radio') inp.checked = String(v) === inp.value;
    else if (document.activeElement !== inp) inp.value = v ?? '';
    showOut(inp);
  });
  const st = cfgVal('style');
  document.querySelectorAll('[data-style]').forEach((inp) => {
    const v = st[inp.dataset.style];
    if (inp.type === 'checkbox') inp.checked = !!v;
    else if (document.activeElement !== inp) inp.value = v;
    showOut(inp);
  });
  document.querySelectorAll('.sw').forEach((b) => b.classList.toggle('on', b.dataset.color.toLowerCase() === String(st.color).toLowerCase()));
  const tr = cfgVal('translator');
  $('s3-deepl').hidden = !(tr === 'deepl' || tr === 'auto');
  $('s3-ollama').hidden = tr !== 'ollama';
}

function readInput(inp) {
  if (inp.type === 'checkbox') return inp.checked;
  if (inp.type === 'number' || inp.type === 'range') return Number(inp.value);
  return inp.value;
}

async function saveConfig(changes, quiet) {
  try {
    const j = await api('config', changes);
    S.conf.config = j.config;
    fillCfg();
    updateDots();
    if (j.restart_needed) {
      toast('A változás a feliratozó következő indításakor lép életbe.', '', { label: 'Újraindítás most', fn: restartEngine });
    } else if (!quiet && j.changed.length) {
      toast('Elmentve.', 'ok');
    }
    return j;
  } catch (e) {
    toast('Nem sikerült menteni: ' + e.message, 'err');
    return null;
  }
}

let pendingStyle = {};
let styleTimer = null;
function saveStyle(key, value) {
  pendingStyle[key] = value;
  clearTimeout(styleTimer);
  styleTimer = setTimeout(() => {
    const style = pendingStyle;
    pendingStyle = {};
    saveConfig({ style }, true);
  }, 150);
}

function bindInputs() {
  document.querySelectorAll('[data-cfg]').forEach((inp) => {
    if (inp.type === 'range') inp.addEventListener('input', () => showOut(inp));
    inp.addEventListener('change', () => {
      if (inp.type === 'radio' && !inp.checked) return;
      saveConfig({ [inp.dataset.cfg]: readInput(inp) }, inp.type === 'checkbox' || inp.type === 'radio');
    });
  });
  document.querySelectorAll('[data-style]').forEach((inp) => {
    inp.addEventListener(inp.type === 'checkbox' ? 'change' : 'input', () => {
      showOut(inp);
      saveStyle(inp.dataset.style, readInput(inp));
    });
  });
  document.querySelectorAll('.sw').forEach((b) => b.addEventListener('click', () => {
    document.querySelector('[data-style="color"]').value = b.dataset.color;
    saveStyle('color', b.dataset.color);
  }));
  document.querySelectorAll('[data-open]').forEach((b) => b.addEventListener('click', () => {
    api('open', { what: b.dataset.open }).catch((e) => toast(e.message, 'err'));
  }));
}

// ------------------------------------------------------------------ állapot, lekérdezés

let pollTimer = null;
let pollBusy = false;
async function poll() {
  clearTimeout(pollTimer);
  if (pollBusy || S.quit) return;
  pollBusy = true;
  try {
    const st = await api('status');
    if (!S.online) {
      S.online = true;
      $('offline').hidden = true;
      loadConfig().catch(() => {});
    }
    renderStatus(st);
  } catch (e) {
    if (S.online) {
      S.online = false;
      $('offline').hidden = false;
      setPill('err', 'Nem érem el a programot');
    }
  } finally {
    pollBusy = false;
  }
  if (S.quit) return;
  const st = S.status;
  const busy = st && (['loading', 'running', 'stopping'].includes(st.engine.state) || st.install.running);
  pollTimer = setTimeout(poll, !S.online ? 2000 : (S.view === 's2' || busy ? 300 : 1000));
}

function setPill(kind, text) {
  const pill = $('pill');
  pill.querySelector('.dot').className = 'dot ' + kind;
  pill.querySelector('span').textContent = text;
}

function setMeter(el, rms, peak) {
  const pct = (db) => Math.max(0, Math.min(100, ((db ?? -120) + 60) / 60 * 100));
  el.querySelector('.meter-cover').style.width = (100 - pct(rms)) + '%';
  el.querySelector('.meter-peak').style.left = `calc(${pct(peak)}% - 3px)`;
}

function setStatusLine(el, kind, text) {
  el.className = 'status-line ' + kind;
  el.textContent = text;
}

function renderStatus(st) {
  S.status = st;
  const e = st.engine;
  const state = e.state;
  const run = state === 'running';
  const isTest = e.mode === 'test';
  setPill({ running: 'run', loading: 'load', stopping: 'load', error: 'err' }[state] || '',
    STATE_TXT[state] + (run && isTest ? ' (próba)' : ''));

  // ---- vezérlőpult: fejléc
  $('d-dot').className = 'state-dot ' + ({ running: 'run', loading: 'load', stopping: 'load', error: 'err' }[state] || '');
  $('d-state').textContent = STATE_TXT[state] + (run && isTest ? ' (próba a mintafelvétellel)' : '');
  let sub = e.finished || 'Indítsd el, amikor szól a játék.';
  if (run) sub = (e.info.input ? 'Bemenet: ' + e.info.input : '') + (e.info.profile ? ' · profil: ' + e.info.profile : '');
  else if (state === 'loading') sub = e.steps.length ? e.steps[e.steps.length - 1].msg : 'Indul…';
  else if (state === 'error') sub = 'Hiba: ' + e.error;
  $('d-sub').textContent = sub;
  const btn = $('d-start');
  btn.disabled = state === 'stopping';
  btn.className = 'btn big ' + (run || state === 'loading' ? 'danger' : 'primary');
  btn.textContent = { running: 'Leállítás', loading: 'Megszakítás', stopping: 'Leáll…' }[state] || 'Indítás';

  // ---- csempék
  $('t-lat').textContent = fmtMs(st.stats.lat_med);
  $('t-asr').textContent = fmtMs(st.stats.asr_med);
  $('t-tr').textContent = fmtMs(st.stats.tr_med);
  $('t-model').textContent = e.info.model || '–';
  $('t-trname').textContent = e.info.translator || TR_NAME[cfgVal('translator')] || '–';
  const usage = (e.info.deepl_limit ? { used: e.info.deepl_used, limit: e.info.deepl_limit } : null)
    || (S.conf && S.conf.deepl.usage);
  if (usage && usage.limit) {
    $('t-deepl').textContent = Math.round(usage.used / usage.limit * 100) + '%';
    $('t-deepl-s').textContent = `${fmtInt(usage.used)} / ${fmtInt(usage.limit)} karakter`;
  } else {
    $('t-deepl').textContent = '–';
    $('t-deepl-s').textContent = S.conf && S.conf.deepl.has_key ? 'induláskor frissül' : 'nincs DeepL-kulcs';
  }

  // ---- hang
  const a = e.audio;
  if (a) {
    setMeter($('d-meter'), a.rms_db, a.peak_db);
    let kind = 'ok';
    let msg = a.msg;
    if (a.error || a.stalled) { kind = 'err'; msg = a.error || 'Nem jön adat a hangbemenetről.'; }
    else if (/^NEM JÖN/.test(msg)) kind = 'err';
    else if (/beszédet nem hallok/.test(msg) || /^várom/.test(msg)) kind = 'warn';
    if (a.lag > 1) msg += ` (lemaradás: ${a.lag.toFixed(1)} mp)`;
    setStatusLine($('d-audio'), kind, msg);
  } else {
    setMeter($('d-meter'), -120, -120);
    setStatusLine($('d-audio'), '', state === 'loading' ? 'Indul…' : 'A feliratozó nem fut.');
  }
  $('d-input').textContent = e.info.input ? 'Bemenet: ' + e.info.input : '';

  // ---- kapcsolók
  if (document.activeElement !== $('d-pause')) $('d-pause').checked = st.paused;
  if (document.activeElement !== $('d-srt')) $('d-srt').checked = !!st.srt;
  $('d-srt-path').textContent = st.srt ? 'Mentés ide: ' + st.srt : 'Pl. az OBS-felvételhez: a mentés indításától számol.';

  // ---- indulás lépései
  const showSteps = state === 'loading' || state === 'error';
  $('d-steps-card').hidden = !showSteps;
  if (showSteps) renderSteps($('d-steps'), e);

  // ---- feliratok
  const hist = st.history || [];
  const key = hist.length ? hist[hist.length - 1].n + ':' + hist.length : '0';
  if (key !== S.histKey) {
    S.histKey = key;
    renderHist($('d-hist'), hist, 'Még nincs felirat.');
    $('d-count').textContent = hist.length ? `${st.stats.lines} mondat` : '';
  }
  renderTrStats(e.translator);

  // ---- 1. lépés: telepítés naplója
  const ins = st.install;
  $('s1-log-card').hidden = !(ins.running || ins.log.length);
  if (ins.log.length) {
    const log = $('s1-log');
    const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
    log.textContent = ins.log.join('\n');
    if (atEnd) log.scrollTop = log.scrollHeight;
    $('s1-log-state').textContent = ins.running ? 'fut…' : (ins.rc === 0 ? 'kész' : 'hiba');
  }
  if (S.installRunning && !ins.running) {
    toast(ins.rc === 0 ? 'A telepítés kész.' : 'A telepítés nem sikerült, lásd a naplót.', ins.rc === 0 ? 'ok' : 'err');
    loadChecks(true);
    loadConfig().catch(() => {});
  }
  S.installRunning = ins.running;

  // ---- 2. lépés: hangszint
  if (S.view === 's2') renderMeterStep(st);

  // ---- 6. lépés: próba
  const badge = $('s6-badge');
  if (isTest && (run || state === 'loading')) { badge.className = 'badge'; badge.textContent = run ? 'fut' : 'indul…'; }
  else if (isTest && state === 'error') { badge.className = 'badge err'; badge.textContent = 'hiba'; }
  else if (isTest && e.finished === 'A próba lefutott.') { badge.className = 'badge ok'; badge.textContent = 'lefutott'; }
  else { badge.className = 'badge'; badge.textContent = ''; badge.hidden = true; }
  badge.hidden = !badge.textContent;
  $('s6-test').disabled = state === 'loading' || state === 'stopping' || (run && isTest);
  $('s6-live').disabled = state === 'loading' || state === 'stopping' || (run && !isTest);
  if (isTest || S.testFromN !== null) {
    renderSteps($('s6-steps'), isTest ? e : { steps: [], error: '' });
    const from = S.testFromN;
    renderHist($('s6-hist'), from === null ? [] : hist.filter((x) => x.n > from), '');
  }
}

function renderSteps(ol, e) {
  const items = e.steps.map((s) => h('li', { text: s.msg }));
  if (e.error && e.state === 'error') items.push(h('li', { class: 'err', text: 'Hiba: ' + e.error }));
  ol.replaceChildren(...items);
}

function renderHist(ul, hist, emptyText) {
  if (!hist.length) {
    ul.replaceChildren(...(emptyText ? [h('li', { class: 'empty', text: emptyText })] : []));
    return;
  }
  ul.replaceChildren(...hist.slice().reverse().map((x) => h('li', null,
    h('span', { class: 'ht', text: x.t }),
    h('div', { class: 'hu', text: x.hu }),
    h('span', { class: 'hl', text: fmtMs(x.lat_ms) }),
    x.en !== x.hu ? h('div', { class: 'en', text: x.en }) : null)));
}

function renderTrStats(t) {
  if (!t || !t.requests) return;
  const parts = [`Elküldve a fordítónak: ${fmtInt(t.sent_chars)} karakter (${fmtInt(t.requests)} kérés).`];
  if (t.spec_requests) {
    parts.push(`Előre fordítás: ${fmtInt(t.spec_requests)} kérés, ebből ${fmtInt(t.spec_hits)} vált be.`);
  }
  parts.push(`Kárba ment: kb. ${t.waste_pct}% (előre fordított, de végül más lett a mondat).`);
  if (t.failures) parts.push(`Fordítási hibák: ${t.failures}.`);
  if (t.waste_pct >= 35) parts.push('Ha fogy a DeepL-keret, a 3. lépésnél kikapcsolhatod az előre fordítást.');
  $('d-trstats').textContent = parts.join(' ');
}

// ------------------------------------------------------------------ indítás, leállítás

async function toggleEngine() {
  const state = S.status ? S.status.engine.state : 'idle';
  try {
    if (state === 'running' || state === 'loading') await api('engine/stop', {});
    else if (state !== 'stopping') await api('engine/start', { mode: 'live' });
  } catch (e) {
    toast(e.message, 'err');
  }
  poll();
}

async function waitIdle() {
  for (let i = 0; i < 60; i++) {
    const st = await api('status');
    if (st.engine.state === 'idle' || st.engine.state === 'error') return;
    await sleep(250);
  }
}

async function restartEngine() {
  try {
    await api('engine/stop', {});
    await waitIdle();
    await api('engine/start', { mode: 'live' });
  } catch (e) {
    toast(e.message, 'err');
  }
  poll();
}

async function startTest() {
  const st = S.status;
  if (st && st.engine.state === 'running' && st.engine.mode !== 'test') {
    if (!confirm('Fut az élő feliratozó. Leállítsam a próba idejére?')) return;
    await api('engine/stop', {});
    await waitIdle();
  }
  const hist = (S.status && S.status.history) || [];
  S.testFromN = hist.length ? hist[hist.length - 1].n : 0;
  try {
    await api('engine/start', { mode: 'test' });
  } catch (e) {
    toast(e.message, 'err');
  }
  poll();
}

function openPlayer() {
  stopPreview();
  api('player/open', {}).then(() => toast('A lejátszó megnyílt (külön ablakban).', 'ok')).catch((e) => toast(e.message, 'err'));
}

async function startLive() {
  try {
    const st = S.status;
    if (st && (st.engine.state === 'running' || st.engine.state === 'loading') && st.engine.mode === 'test') {
      await api('engine/stop', {});
      await waitIdle();
    }
    stopPreview();
    await saveConfig({ setup_done: true }, true);
    await api('player/open', { start: true });
    show('dash');
  } catch (e) {
    toast(e.message, 'err');
  }
  poll();
}

// ------------------------------------------------------------------ 1. telepítés

async function loadChecks(refresh) {
  const ul = $('s1-checks');
  if (refresh || !S.checks) ul.replaceChildren(h('li', { class: 'loading', text: 'Ellenőrzés… (az első alkalommal pár másodperc)' }));
  try {
    S.checks = await api('checks' + (refresh ? '?refresh=1' : ''));
  } catch (e) {
    ul.replaceChildren(h('li', { class: 'loading', text: 'Az ellenőrzés nem sikerült: ' + e.message }));
    return;
  }
  renderChecks();
  updateDots();
}

function checkRow(kind, title, detail) {
  return h('li', null, h('span', { class: 'ci ' + kind, text: { ok: '✓', warn: '!', err: '✕' }[kind] }),
    h('b', { text: title }), detail ? h('span', { class: 'cd', text: detail }) : null);
}

function renderChecks() {
  const c = S.checks;
  const ul = $('s1-checks');
  if (c.error) { ul.replaceChildren(checkRow('err', 'Az ellenőrzés nem sikerült', c.error)); return; }
  const rows = [];
  rows.push(checkRow(c.python.ok ? 'ok' : 'warn', `Python ${c.python.version} (${c.python.bits} bites)`,
    c.python.ok ? '' : 'A Python 3.10–3.12 ajánlott (64 bites).'));
  const missing = c.packages.filter((p) => !p.ok).map((p) => p.name);
  rows.push(checkRow(missing.length ? 'err' : 'ok',
    missing.length ? 'Hiányzó csomagok: ' + missing.join(', ') : 'A szükséges csomagok telepítve',
    missing.length ? 'Telepítsd lent az „Alapcsomagok telepítése” gombbal (pár perc).'
      : c.packages.map((p) => `${p.name} ${p.version || ''}`).join(' · ')));
  const g = c.gpu;
  if (!g.nvidia) rows.push(checkRow('warn', 'Nem találok NVIDIA videokártyát', 'CPU-n is fut, kisebb modellel, de lassabb és pontatlanabb.'));
  else if (!g.libs) rows.push(checkRow('err', 'NVIDIA: ' + g.name, 'Hiányoznak a GPU-gyorsítás könyvtárai (CUDA 12 / cuDNN 9, kb. 1–2 GB). Telepítsd lent.'));
  else if (!g.cuda_devices) rows.push(checkRow('warn', 'NVIDIA: ' + g.name, 'A CUDA nem látja a kártyát: frissítsd a videokártya illesztőprogramját.'));
  else rows.push(checkRow('ok', 'NVIDIA: ' + g.name, 'GPU-gyorsítás rendben.'));
  rows.push(checkRow(c.model.cached ? 'ok' : 'warn', 'Felismerő modell: ' + c.model.name,
    c.model.cached ? 'Letöltve.' : 'Az első indításkor letölti (egyszeri, kb. 1,5 GB).'));
  rows.push(checkRow(c.audio.cards.length ? 'ok' : 'warn',
    c.audio.cards.length ? 'Capture kártya: ' + c.audio.cards.join(', ') : 'Nem találok képpel együtt érkező kártyahangot',
    c.audio.cards.length ? 'A kép és a hang innen jön, OBS nélkül.'
      : (c.audio.error || 'USB-s kártyánál a hang külön Windows-hangbemenet: a 2. lépésnél kiválaszthatod.')));
  rows.push(checkRow(c.ffmpeg.ok ? 'ok' : 'err', c.ffmpeg.ok ? 'ffmpeg megvan' : 'Nincs ffmpeg',
    c.ffmpeg.ok ? 'A kártya hangját ez olvassa ki.' : 'A kártya hangjához kell: az „Alapcsomagok telepítése” felteszi.'));
  rows.push(checkRow(c.browser.ok ? 'ok' : 'warn', c.browser.ok ? 'Microsoft Edge megvan' : 'Nem találom a Microsoft Edge-et',
    c.browser.ok ? 'Ebben fut a lejátszó és ez az ablak.' : 'A lejátszó a szokásos böngésződben nyílik meg; a „mindig legfelül” így nem biztos.'));
  if (!c.path_ascii) {
    rows.push(checkRow('warn', 'Ékezet van a projektmappa útvonalában',
      'Egyes könyvtárak ezt nem szeretik. Ha hibát látsz, tedd át a mappát pl. ide: E:\\rksubbyV2'));
  }
  rows.push(checkRow(c.pyqt5 ? 'ok' : 'warn', c.pyqt5 ? 'PyQt5 telepítve (nem kötelező)' : 'PyQt5 nincs telepítve (nem kötelező)',
    'Az átlátszó felirat-réteghez kell (5. lépés).'));
  ul.replaceChildren(...rows);

  const act = $('s1-actions');
  const busy = !!(S.status && S.status.install.running);
  act.replaceChildren();
  if (missing.length) act.append(h('button', { class: 'btn primary', type: 'button', disabled: busy, onclick: () => install('base') }, 'Alapcsomagok telepítése'));
  if (g.nvidia && !g.libs) act.append(h('button', { class: missing.length ? 'btn ghost' : 'btn primary', type: 'button', disabled: busy, onclick: () => install('gpu') }, 'NVIDIA-gyorsítás telepítése'));
  if (!c.pyqt5) act.append(h('button', { class: 'btn ghost small', type: 'button', disabled: busy, onclick: () => install('pyqt5') }, 'PyQt5 telepítése'));
}

async function install(what) {
  try {
    await api('install', { what });
    S.installRunning = true;
    $('s1-log-card').hidden = false;
    document.querySelectorAll('#s1-actions .btn').forEach((b) => { b.disabled = true; });
    toast('A telepítés elindult. A napló lent látszik.');
  } catch (e) {
    toast(e.message, 'err');
  }
  poll();
}

// ------------------------------------------------------------------ 2. forrás: kép és hang

const CAPTURE_RE = /avermedia|live gamer|gc5\d\d|capture|hdmi|elgato|cam link|usb video|magewell|razer ripsaw/i;
let preview = null;

async function videoInputs() {
  let devs = await navigator.mediaDevices.enumerateDevices();
  if (!devs.some((d) => d.kind === 'videoinput' && d.label)) {
    // a nevek csak engedély után és nyitott képforrás mellett látszanak
    const tmp = await navigator.mediaDevices.getUserMedia({ video: true });
    try {
      devs = await navigator.mediaDevices.enumerateDevices();
    } finally {
      tmp.getTracks().forEach((t) => t.stop());
    }
  }
  return devs.filter((d) => d.kind === 'videoinput' && d.label && d.deviceId);
}

async function loadVideo() {
  const sel = $('s2-video');
  let vids = [];
  try {
    vids = await videoInputs();
  } catch (e) {
    sel.replaceChildren(h('option', { value: '' }, 'Nincs engedély a képforrásokhoz'));
    $('s2-vmsg').textContent = 'Engedélyezd a kamerát/képforrást ennek az ablaknak, majd nyomd meg a Frissítés gombot. (' + e.message + ')';
    $('s2-vmsg').hidden = false;
    return;
  }
  S.videos = vids;
  const cur = cfgVal('video_device') || '';
  let chosen = vids.find((d) => d.label === cur) || (cur && vids.find((d) => d.label.includes(cur)));
  if (!cur && !chosen) chosen = vids.find((d) => CAPTURE_RE.test(d.label));
  sel.replaceChildren(...vids.map((d) => h('option', { value: d.label, selected: chosen === d },
    d.label + (CAPTURE_RE.test(d.label) ? '  · capture kártya' : ''))));
  if (!vids.length) sel.append(h('option', { value: '' }, 'Nem találok képforrást'));
  else if (!chosen) sel.prepend(h('option', { value: '', selected: true }, cur ? cur + ' (nem található)' : '– válassz képforrást –'));
  const res = `${cfgVal('video_res')}@${cfgVal('video_fps')}`;
  if ([...$('s2-res').options].some((o) => o.value === res)) $('s2-res').value = res;
  if (chosen && chosen.label !== cur) await chooseVideo(chosen.label);   // első alkalom: a kártyát magától kiválasztja
  else startPreview();
}

async function chooseVideo(label) {
  await saveConfig({ video_device: label }, true);
  // a képpel azonos nevű capture kártya hangja is legyen a hangforrás (pl. AVerMedia GC573: kép + hang egy eszköz)
  const src = (S.devices && S.devices.sources || []).find((d) => d.kind === 'dshow' && (d.name === label || label.includes(d.name) || d.name.includes(label)));
  if (src && !(cfgVal('audio_kind') === 'dshow' && cfgVal('dshow_device') === src.name)) {
    await saveConfig({ audio_kind: 'dshow', dshow_device: src.name }, true);
    renderSources();
    startMeter(true);
    toast('A kártya hangját is kiválasztottam hangforrásnak.', 'ok');
  }
  startPreview();
}

function stopPreview() {
  if (preview) preview.getTracks().forEach((t) => t.stop());
  preview = null;
  $('s2-preview').srcObject = null;
}

async function startPreview() {
  stopPreview();
  const label = cfgVal('video_device');
  const dev = (S.videos || []).find((d) => d.label === label);
  const msg = $('s2-vmsg');
  if (!dev) { msg.textContent = 'Válassz képforrást.'; msg.hidden = false; return; }
  const [w, h2] = String(cfgVal('video_res')).split('x').map(Number);
  msg.textContent = 'Kép betöltése…';
  msg.hidden = false;
  try {
    preview = await navigator.mediaDevices.getUserMedia({ audio: false,
      video: { deviceId: { exact: dev.deviceId }, width: { ideal: w }, height: { ideal: h2 }, frameRate: { ideal: cfgVal('video_fps') } } });
  } catch (e) {
    msg.textContent = `A képforrás nem nyitható meg: ${e.message}`;
    return;
  }
  if (S.view !== 's2') { stopPreview(); return; }
  $('s2-preview').srcObject = preview;
  msg.hidden = true;
  const st = preview.getVideoTracks()[0].getSettings();
  $('s2-vinfo').textContent = `${st.width} × ${st.height}, ${Math.round(st.frameRate || 0)} fps`;
}

async function loadDevices() {
  try {
    S.devices = await api('devices');
  } catch (e) {
    S.devices = { sources: [], outputs: [], errors: [e.message] };
  }
  renderSources();
  const out = $('s2-out');
  const curOut = cfgVal('monitor_device') || '';
  out.replaceChildren(h('option', { value: '', selected: !curOut }, 'A Windows alapértelmezett kimenete'),
    ...(S.devices.outputs || []).map((d) => h('option', { value: d.name, selected: d.name === curOut }, d.name)));
  updateDots();
}

function sourceKey(kind, name) { return kind + '|' + name; }

function renderSources() {
  const sel = $('s2-audio');
  const list = (S.devices && S.devices.sources) || [];
  const kind = cfgVal('audio_kind');
  const cur = (kind === 'dshow' ? cfgVal('dshow_device') : cfgVal('device_name')) || '';
  let found = false;
  const opts = list.map((d) => {
    const isSel = !found && d.kind === kind && !!cur && d.name.toLowerCase().includes(cur.toLowerCase());
    found = found || isSel;
    const note = d.card ? '  · capture kártya (kép + hang)' : (d.cable ? '  · OBS-hez (VB-CABLE)' : '');
    return h('option', { value: sourceKey(d.kind, d.name), selected: isSel }, d.name + note);
  });
  sel.replaceChildren(...opts);
  if (!list.length) {
    sel.append(h('option', { value: '' }, (S.devices && S.devices.errors && S.devices.errors[0]) || 'Nincs hangforrás'));
  } else if (!found) {
    sel.prepend(h('option', { value: '', selected: true }, cur ? cur + ' (nem található)' : '– válassz hangforrást –'));
  }
}

async function chooseSource() {
  const v = $('s2-audio').value;
  if (!v) return;
  const i = v.indexOf('|');
  const kind = v.slice(0, i);
  const name = v.slice(i + 1);
  const ch = kind === 'dshow' ? { audio_kind: 'dshow', dshow_device: name } : { audio_kind: 'pa', device_name: name, device_index: null };
  await saveConfig(ch, true);
  startMeter(true);
}

function startMeter(restart) {
  const st = S.status;
  if (st && ['loading', 'running'].includes(st.engine.state)) return;
  const go = () => api('meter', { on: true }).catch((e) => setStatusLine($('s2-status'), 'err', e.message));
  if (restart) api('meter', { on: false }).then(go, go);
  else go();
}

function stopMeter() {
  api('meter', { on: false }).catch(() => {});
}

function renderMeterStep(st) {
  const line = $('s2-status');
  const e = st.engine;
  if (['loading', 'running'].includes(e.state)) {
    if (e.audio) setMeter($('s2-meter'), e.audio.rms_db, e.audio.peak_db);
    setStatusLine(line, 'ok', 'Fut a feliratozó: ezt a forrást hallgatja (a részletek a vezérlőpulton).');
    return;
  }
  const m = st.meter;
  if (!m) { setMeter($('s2-meter'), -120, -120); return; }
  setMeter($('s2-meter'), m.rms_db, m.peak_db);
  let extra = m.monitor ? ` · szól itt: ${m.monitor}` : (m.monitor_error ? ` · ${m.monitor_error}` : '');
  if (!cfgVal('monitor_audio')) extra = '';
  if (m.error) setStatusLine(line, 'err', 'Hiba: ' + m.error);
  else if (!m.receiving) setStatusLine(line, 'warn', 'Kapcsolódás a hangforráshoz…');
  else if (m.peak_db < -55) setStatusLine(line, 'warn', 'Csend. Indíts el valamit a konzolon, aminek van hangja (videó, játék).' + extra);
  else setStatusLine(line, 'ok', `Jön hang ✓  (csúcs ${Math.round(m.peak_db)} dB)` + extra);
}

// ------------------------------------------------------------------ 3. fordítás

function renderDeepl() {
  if (!S.conf) return;
  const d = S.conf.deepl;
  const badge = $('s3-key-badge');
  const quota = $('s3-quota');
  if (d.has_key) {
    badge.className = 'badge ok';
    badge.textContent = d.free ? 'Free kulcs' : 'Pro kulcs';
    let info = 'Kulcs: ' + d.masked + (d.source === 'env' ? ' (a DEEPL_API_KEY környezeti változóból)' : '');
    if (d.usage) info += ` · havi keret: ${fmtInt(d.usage.used)} / ${fmtInt(d.usage.limit)} karakter felhasználva`;
    $('s3-key-info').textContent = info;
    quota.hidden = !(d.usage && d.usage.limit);
    if (d.usage && d.usage.limit) quota.firstElementChild.style.width = Math.min(100, d.usage.used / d.usage.limit * 100) + '%';
  } else {
    badge.className = 'badge err';
    badge.textContent = 'nincs kulcs';
    $('s3-key-info').textContent = 'Még nincs kulcs beállítva. Kulcs nélkül a tartalék fordító (ha fut az Ollama) vagy az angol szöveg jelenik meg.';
    quota.hidden = true;
  }
}

function refreshUsage() {
  if (!S.conf || !S.conf.deepl.has_key || S.conf.deepl.usage) return;
  api('deepl/usage', {}).then((j) => { S.conf.deepl.usage = j.usage; renderDeepl(); }).catch(() => {});
}

function renderOllama() {
  const o = S.checks && S.checks.ollama;
  if (!o) return;
  const info = $('s3-ollama-info');
  if (o.running && o.model) info.textContent = `Az Ollama fut, a fordítómodell megvan: ${o.model}.`;
  else if (o.running) info.textContent = 'Az Ollama fut, de nincs letöltve fordítómodell. Parancssorban: ollama pull translategemma:12b';
}

async function saveKey() {
  const key = $('s3-key').value.trim();
  if (!key) { toast('Illeszd be a kulcsot a mezőbe (Ctrl+V).', 'err'); return; }
  const btn = $('s3-key-save');
  btn.disabled = true;
  btn.textContent = 'Ellenőrzés…';
  try {
    const j = await api('deepl/key', { key });
    $('s3-key').value = '';
    Object.assign(S.conf.deepl, { has_key: true, masked: j.masked, free: j.free, usage: j.usage, source: 'file' });
    renderDeepl();
    updateDots();
    toast(j.warning || 'A kulcs működik, elmentettem.', j.warning ? '' : 'ok');
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Ellenőrzés és mentés';
  }
}

async function testTranslate() {
  const out = $('s3-test-out');
  out.className = 'result';
  out.textContent = 'Fordítás…';
  try {
    const j = await api('translate/test', { text: $('s3-test-text').value });
    if (j.ok) {
      out.className = 'result ok';
      out.textContent = `${j.text}   (${j.name}, ${j.ms} ms)`;
    } else {
      out.className = 'result err';
      out.textContent = `${j.name}: ${j.error}`;
    }
    if (j.usage) { S.conf.deepl.usage = j.usage; renderDeepl(); }
  } catch (e) {
    out.className = 'result err';
    out.textContent = e.message;
  }
}

// ------------------------------------------------------------------ 4. játékprofil

function renderProfiles() {
  const sel = $('s4-profile');
  sel.replaceChildren(...S.conf.profiles.map((p) => h('option', { value: p.id, selected: p.id === cfgVal('profile') },
    p.name + (p.customized ? ' (módosítva)' : (p.builtin ? '' : ' (saját)')))));
  fillProfile(S.conf.profile);
}

function fillProfile(p) {
  S.profile = p;
  $('s4-name').value = p.name;
  $('s4-hotwords').value = p.hotwords;
  $('s4-hint').value = p.llm_hint;
  const tb = $('s4-gloss');
  tb.replaceChildren(...p.glossary.map(([en, hu]) => glossRow(en, hu)));
  if (!p.glossary.length) tb.append(glossRow('', ''));
  $('s4-reset').textContent = p.builtin ? 'Visszaállítás' : 'Profil törlése';
}

function glossRow(en, hu) {
  return h('tr', null,
    h('td', null, h('input', { type: 'text', value: en, placeholder: 'angol (pl. cops)', 'aria-label': 'angol kifejezés' })),
    h('td', { text: '→' }),
    h('td', null, h('input', { type: 'text', value: hu, placeholder: 'magyar (pl. zsaruk)', 'aria-label': 'magyar fordítás' })),
    h('td', null, h('button', { class: 'x', type: 'button', title: 'Sor törlése', onclick: (ev) => ev.currentTarget.closest('tr').remove() }, '×')));
}

function profileFromForm() {
  const glossary = [...$('s4-gloss').querySelectorAll('tr')]
    .map((tr) => [...tr.querySelectorAll('input')].map((i) => i.value.trim()))
    .filter(([en, hu]) => en && hu);
  return { id: S.profile.id, name: $('s4-name').value.trim(), hotwords: $('s4-hotwords').value.trim(),
    llm_hint: $('s4-hint').value.trim(), glossary };
}

function applyProfileResult(j) {
  S.conf.profiles = j.profiles;
  S.conf.profile = j.profile;
  if (j.config) S.conf.config = j.config;
  else S.conf.config.profile = j.profile.id;
  renderProfiles();
}

async function changeProfile() {
  const id = $('s4-profile').value;
  try {
    fillProfile(await api('profile?id=' + encodeURIComponent(id)));
    S.conf.profile = S.profile;
    await saveConfig({ profile: id }, true);
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function saveProfile() {
  try {
    const j = await api('profile/save', profileFromForm());
    applyProfileResult(j);
    if (j.restart_needed) toast('Elmentve. A feliratozó következő indításakor lép életbe.', 'ok', { label: 'Újraindítás most', fn: restartEngine });
    else toast('A profil elmentve.', 'ok');
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function newProfile() {
  const name = (prompt('Az új profil neve (pl. a játék címe):') || '').trim();
  if (!name) return;
  try {
    applyProfileResult(await api('profile/save', { new: true, name, hotwords: '', llm_hint: name, glossary: [] }));
    toast('Új profil: ' + name + '. Add meg a neveket és a kifejezéseket, aztán mentsd.', 'ok');
    $('s4-hotwords').focus();
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function resetProfile() {
  const p = S.profile;
  if (!confirm(p.builtin ? `Visszaállítod a(z) „${p.name}” profil eredeti beállításait?` : `Törlöd a(z) „${p.name}” profilt?`)) return;
  try {
    applyProfileResult(await api('profile/reset', { id: p.id }));
    toast(p.builtin ? 'Visszaállítva.' : 'Törölve.', 'ok');
  } catch (e) {
    toast(e.message, 'err');
  }
}

// ------------------------------------------------------------------ 5. OBS

function fitPreview() {
  const box = $('s5-preview');
  const fr = $('s5-frame');
  if (!fr.getAttribute('src')) fr.setAttribute('src', '/?demo=1');
  if (box.clientWidth) fr.style.transform = `scale(${box.clientWidth / 1920})`;
}

async function copyUrl() {
  const inp = $('s5-url');
  try {
    await navigator.clipboard.writeText(inp.value);
  } catch (e) {
    inp.select();
    document.execCommand('copy');
  }
  toast('A cím a vágólapon. Illeszd be az OBS böngészőforrás Webcím mezőjébe.', 'ok');
}

// ------------------------------------------------------------------ jelzőpöttyök

function setDot(id, kind) { $(id).className = 'dot ' + (kind || ''); }

function updateDots() {
  const c = S.checks && !S.checks.error ? S.checks : null;
  if (c) setDot('dot-s1', !c.base_ok ? 'err' : (c.gpu.nvidia && !c.gpu.ok ? 'warn' : 'ok'));
  else if (S.conf) setDot('dot-s1', S.conf.base_ok ? '' : 'err');
  const cur = ((cfgVal('audio_kind') === 'dshow' ? cfgVal('dshow_device') : cfgVal('device_name')) || '').toLowerCase();
  if (S.devices && (S.devices.sources || []).length) {
    setDot('dot-s2', S.devices.sources.some((d) => d.kind === cfgVal('audio_kind') && cur && d.name.toLowerCase().includes(cur)) ? 'ok' : 'warn');
  } else if (c) setDot('dot-s2', c.audio.selected_found ? 'ok' : 'warn');
  if (!S.conf) return;
  const tr = cfgVal('translator');
  setDot('dot-s3', tr === 'deepl' || tr === 'auto' ? (S.conf.deepl.has_key ? 'ok' : 'err') : (tr === 'none' ? 'warn' : 'ok'));
  setDot('dot-s4', 'ok');
  setDot('dot-s5', 'ok');
  setDot('dot-s6', cfgVal('setup_done') ? 'ok' : '');
}

// ------------------------------------------------------------------ indulás

async function quitApp() {
  if (!confirm('Kilépsz a programból? A feliratozó leáll, és az OBS-ben sem lesz felirat.')) return;
  try { await api('quit', {}); } catch (e) { /* már leállt */ }
  S.quit = true;
  $('bye').hidden = false;
  setTimeout(() => window.close(), 700);
}

function bindButtons() {
  $('d-start').addEventListener('click', toggleEngine);
  $('d-pause').addEventListener('change', (ev) => api('subs/pause', { paused: ev.target.checked }).catch((e) => toast(e.message, 'err')));
  $('d-srt').addEventListener('change', async (ev) => {
    try {
      const j = await api('srt', { on: ev.target.checked });
      if (j.srt) toast('SRT-mentés: ' + j.srt, 'ok');
    } catch (e) { toast(e.message, 'err'); }
    poll();
  });
  $('d-clear').addEventListener('click', () => api('subs/clear', {}).catch((e) => toast(e.message, 'err')));
  $('s1-recheck').addEventListener('click', () => loadChecks(true));
  $('s2-audio').addEventListener('change', chooseSource);
  $('s2-refresh').addEventListener('click', () => loadDevices().then(() => startMeter(true)));
  $('s2-video').addEventListener('change', () => { if ($('s2-video').value) chooseVideo($('s2-video').value); });
  $('s2-vrefresh').addEventListener('click', loadVideo);
  $('s2-res').addEventListener('change', async () => {
    const [res, fps] = $('s2-res').value.split('@');
    await saveConfig({ video_res: res, video_fps: Number(fps) }, true);
    startPreview();
  });
  $('s2-out').addEventListener('change', async () => {
    await saveConfig({ monitor_device: $('s2-out').value }, true);
    startMeter(true);
  });
  $('s5-player').addEventListener('click', openPlayer);
  $('d-player').addEventListener('click', openPlayer);
  $('s3-key-save').addEventListener('click', saveKey);
  $('s3-key').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') saveKey(); });
  $('s3-test').addEventListener('click', testTranslate);
  $('s4-profile').addEventListener('change', changeProfile);
  $('s4-new').addEventListener('click', newProfile);
  $('s4-reset').addEventListener('click', resetProfile);
  $('s4-save').addEventListener('click', saveProfile);
  $('s4-gloss-add').addEventListener('click', () => { const tr = glossRow('', ''); $('s4-gloss').append(tr); tr.querySelector('input').focus(); });
  $('s5-copy').addEventListener('click', copyUrl);
  $('s5-default').addEventListener('click', () => saveConfig({ style: S.conf.default_style }, true));
  $('s6-test').addEventListener('click', startTest);
  $('s6-live').addEventListener('click', startLive);
  $('quit').addEventListener('click', quitApp);
  new ResizeObserver(fitPreview).observe($('s5-preview'));
}

async function init() {
  bindInputs();
  bindButtons();
  try {
    await loadConfig();
  } catch (e) {
    S.online = false;
    $('offline').hidden = false;
  }
  show(location.hash.slice(1));
  poll();
  if (S.view !== 's1') loadChecks(false);   // a jelzőpöttyökhöz, a háttérben
}

init();
