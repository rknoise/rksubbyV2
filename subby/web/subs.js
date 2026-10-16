/* rksubby felirat-megjelenítő: az OBS-oldal (/) és a lejátszó (/player) közös része.
   A felirat élőben, Server-Sent Events-szel érkezik (/events). Ha a program nem fut, nem marad kint semmi. */
(function () {
  'use strict';

  var DEMO = [
    { id: 'd1', hu: 'Szállj be a kocsiba, Franklin, mielőtt ideérnek a zsaruk!',
      en: 'Get in the car, Franklin, before the cops get here!' },
    { id: 'd2', hu: 'Nyugi, haver. Megoldom.', en: 'Relax, man. I got this.' }
  ];

  function mount(box, opts) {
    opts = opts || {};
    var shown = {};          // azonosító -> elem
    var partialEl = null;
    var styleKey = '';
    var onState = opts.onState || function () {};

    function applyStyle(st) {
      var key = JSON.stringify(st);
      if (key === styleKey) return;
      styleKey = key;
      var r = document.documentElement.style;
      if (st.font) r.setProperty('--font', '"' + st.font + '", Arial, sans-serif');
      if (st.font_size) r.setProperty('--size', st.font_size + 'px');
      if (st.color) r.setProperty('--color', st.color);
      if (st.en_color) r.setProperty('--en', st.en_color);
      if (st.max_width) r.setProperty('--maxw', st.max_width + 'px');
      if (st.bottom !== undefined) r.setProperty('--bottom', st.bottom + 'px');
      r.setProperty('--box', st.box ? 'rgba(0,0,0,' + st.box_opacity + ')' : 'transparent');
    }

    function fill(el, ln) {
      var key = ln.hu + '\u0001' + (ln.en || '');
      if (el._key === key) return;
      el._key = key;
      el.textContent = '';
      var hu = document.createElement('div');
      hu.className = 'hu';
      hu.textContent = ln.hu;
      el.appendChild(hu);
      if (ln.en) {
        var en = document.createElement('div');
        en.className = 'en';
        en.textContent = ln.en;
        el.appendChild(en);
      }
    }

    function render(state) {
      if (state.style) applyStyle(state.style);
      var lines = state.lines || [];
      if (opts.demo) {
        lines = DEMO.map(function (d) { return { id: d.id, hu: d.hu, en: state.show_en ? d.en : '' }; });
      }
      var keep = {};
      lines.forEach(function (ln) { keep[ln.id] = true; });
      Object.keys(shown).forEach(function (id) {
        if (!keep[id]) { box.removeChild(shown[id]); delete shown[id]; }
      });
      lines.forEach(function (ln) {
        var el = shown[ln.id];
        if (!el) { el = document.createElement('div'); el.className = 'line'; shown[ln.id] = el; }
        fill(el, ln);
        box.appendChild(el);   // a meglévőt a helyére teszi: a sorrend mindig a kapott sorrend
      });
      var p = opts.demo ? '' : (state.partial || '');
      if (p) {
        if (!partialEl) { partialEl = document.createElement('div'); partialEl.className = 'partial'; }
        partialEl.textContent = p;
        box.appendChild(partialEl);
      } else if (partialEl && partialEl.parentNode) {
        box.removeChild(partialEl);
      }
      onState(state);
    }

    function clearAll() { if (!opts.demo) render({ lines: [] }); }

    function poll() {   // tartalék régi böngészőkhöz
      fetch('/state?t=' + Date.now(), { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(render)
        .catch(clearAll)
        .then(function () { setTimeout(poll, 150); });
    }

    function connect() {
      if (!window.EventSource) { poll(); return; }
      var es = new EventSource('/events');
      var last = Date.now();
      es.onmessage = function (e) { last = Date.now(); render(JSON.parse(e.data)); };
      es.addEventListener('ping', function () { last = Date.now(); });
      es.onerror = clearAll;   // a böngésző magától újracsatlakozik
      var timer = setInterval(function () {   // beragadt kapcsolat: újranyitjuk
        if (Date.now() - last > 40000) { clearInterval(timer); es.close(); clearAll(); connect(); }
      }, 5000);
    }

    connect();
    return { render: render };
  }

  window.RkSubs = { mount: mount };
})();
