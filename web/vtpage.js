// VT page: the screen of the virtual terminal with working set designators, the active data or
// alarm mask and the soft keys; operator input goes back to the implement over CAN.
import { h, $, hex, fmt, parseNum, send, on, toast } from './util.js';
import { renderObject, sizeOf, keyOf, enabled, formatNumber, NONE } from './vt/render.js';

const LOCAL = 0x100;
let root, frame, maskEl, skEl, wsEl, infoEl, listEl, logEl;
let vt = { ws: [], cfg: { dmSize: 480, skWidth: 80, skHeight: 80, skCount: 6 } };
const pools = new Map(); // addr → objects
const skPage = new Map(); // `${addr}:${skm}` → page
let pending = false;
let logRows = [];
let logPaused = false;
let markInputs = store('vt.markInputs') !== '0';
let dialog = null;
let audioCtx = null;
let volume = 1;

function store(k, v) {
  try {
    if (v === undefined) return localStorage.getItem(k);
    localStorage.setItem(k, v);
  } catch { /* storage not available */ }
  return null;
}

export function mount(el) {
  root = el;
  maskEl = h('div', { class: 'vt-mask' });
  skEl = h('div', { class: 'vt-skcol' });
  wsEl = h('div', { class: 'vt-wscol' });
  frame = h('div', { class: 'vt-frame' }, wsEl, maskEl, skEl);
  infoEl = h('div', { class: 'vt-bar' });
  listEl = h('div', { class: 'tablewrap' });
  logEl = h('div', { class: 'vt-log' });
  const fileIn = h('input', { type: 'file', accept: '.iop,application/octet-stream', style: { display: 'none' }, onchange: openFile });
  root.append(h('div', { class: 'vt-layout' },
    h('div', { class: 'vt-main' }, infoEl, h('div', { class: 'vt-stage' }, frame)),
    h('aside', { class: 'vt-side' },
      h('section', { class: 'card' }, h('h3', null, 'Werksets'), listEl,
        h('div', { class: 'toolbar small' },
          h('button', { class: 'btn ghost', onclick: () => fileIn.click(), title: 'Een .iop-bestand tonen zonder werktuig (bv. uit PLC-VT-Designer)' }, 'Pool openen…'),
          h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: markInputs, onchange: (e) => { markInputs = e.target.checked; store('vt.markInputs', markInputs ? '1' : '0'); draw(); } }), 'invoervelden markeren'),
          fileIn)),
      h('section', { class: 'card grow' }, h('h3', null, 'VT-berichten',
        h('span', { class: 'tools' },
          h('button', { class: 'btn small ghost', onclick: (e) => { logPaused = !logPaused; e.target.textContent = logPaused ? 'Verder' : 'Pauze'; } }, 'Pauze'),
          h('button', { class: 'btn small ghost', onclick: () => { logRows = []; renderLog(); } }, 'Wissen'))), logEl))));

  on('vt', (m) => { vt = m; if (!m.enabled) { maskEl.replaceChildren(h('div', { class: 'vt-empty' }, 'De virtuele terminal staat uit (Instellingen).')); return; } schedule(); renderList(); syncEdit(); });
  on('pool', (m) => { pools.set(m.addr, m.objects); schedule(); });
  on('objs', (m) => {
    const O = pools.get(m.addr);
    if (!O) return;
    for (const o of m.objects) O[o.id] = o;
    schedule();
  });
  on('vt.log', (m) => {
    if (!m.append) logRows = [];
    if (!logPaused || !m.append) logRows.push(...m.rows);
    if (logRows.length > 400) logRows = logRows.slice(-400);
    renderLog();
  });
  on('vt.audio', audio);
  on('vt.edit', (m) => {
    if (m.id === null || m.id === undefined) closeDialog(false);
    else openInput(m.addr, m.id, true);
  });
  on('vt.identify', (m) => {
    const el = h('div', { class: 'vt-identify' }, `VT ${m.instance + 1}`);
    frame.append(el);
    setTimeout(() => el.remove(), 3000);
  });

  maskEl.addEventListener('pointerdown', maskDown);
  document.addEventListener('pointerdown', () => { if (!audioCtx && window.AudioContext) audioCtx = new AudioContext(); }, { once: true });
  new ResizeObserver(() => scale()).observe(root);
}

export function show() { scale(); }

const activeWs = () => vt.ws?.find((w) => w.addr === vt.active);
const P = (addr) => ({ objects: pools.get(addr) || {}, settings: vt.cfg, selected: vt.ws?.find((w) => w.addr === addr)?.selected, markInputs });

function schedule() {
  if (pending) return;
  pending = true;
  const run = () => { if (pending) { pending = false; draw(); } };
  requestAnimationFrame(run);
  setTimeout(run, 150); // requestAnimationFrame pauses in a hidden tab
}

function scale() {
  if (!frame || !root.offsetParent) return;
  const c = vt.cfg;
  const natural = c.skWidth * 2 + c.dmSize + 40;
  const stage = frame.parentElement;
  const avail = stage.clientWidth - 8;
  const availH = window.innerHeight - stage.getBoundingClientRect().top - 24;
  const s = Math.max(0.35, Math.min(1.6, avail / natural, availH > 200 ? availH / (c.dmSize + 20) : 9));
  frame.style.setProperty('--s', s);
  frame.style.setProperty('--dm', c.dmSize + 'px');
  frame.style.setProperty('--skw', c.skWidth + 'px');
  frame.style.setProperty('--skh', c.skHeight + 'px');
}

function svg(markup, w, h, cls = '') {
  return `<svg class="vt ${cls}" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" xmlns="http://www.w3.org/2000/svg">${markup}</svg>`;
}

// ---------- drawing
function draw() {
  if (!vt.enabled) return;
  scale();
  drawDesignators();
  const ws = activeWs();
  const c = vt.cfg;
  renderInfo(ws);
  if (!ws || ws.state !== 'actief' || !pools.get(ws.addr)) {
    const up = vt.ws.find((w) => w.progress || w.state === 'uploaden');
    let msg = 'Geen werkset actief. Sluit een werktuig aan of open een pool.';
    if (up?.progress) msg = `Object pool van 0x${hex(up.addr)} wordt geladen… ${Math.round((100 * up.progress.got) / up.progress.size)} %`;
    else if (up) msg = `Werkset 0x${hex(up.addr)} laadt zijn pool…`;
    else if (vt.ws.some((w) => w.state === 'fout')) msg = 'Pool geweigerd: zie Werksets.';
    const pct = up?.progress ? (100 * up.progress.got) / up.progress.size : null;
    maskEl.replaceChildren(h('div', { class: 'vt-empty' }, h('div', null, msg), pct !== null ? h('div', { class: 'bar' }, h('i', { style: { width: pct + '%' } })) : null));
    drawSoftKeys(null);
    return;
  }
  if (ws.locked && maskEl.querySelector('svg')) return;
  const p = P(ws.addr);
  const { markup } = renderObject(p, ws.activeMask, c.dmSize, c.dmSize);
  maskEl.innerHTML = svg(markup, c.dmSize, c.dmSize, 'mask');
  maskEl.dataset.addr = ws.addr;
  drawSoftKeys(ws);
  markPressed();
}

function drawDesignators() {
  const c = vt.cfg;
  const list = (vt.ws || []).filter((w) => w.state !== 'fout' || true);
  wsEl.replaceChildren(...list.map((w) => {
    const O = pools.get(w.addr);
    let inner;
    if (w.state === 'actief' && O && O[w.wsId]) {
      inner = svg(renderObject(P(w.addr), w.wsId, c.skWidth, c.skHeight).markup, c.skWidth, c.skHeight);
    } else {
      const t = w.state === 'uploaden' && w.progress ? `${Math.round((100 * w.progress.got) / w.progress.size)} %` : w.state;
      inner = svg(`<rect width="${c.skWidth}" height="${c.skHeight}" fill="#3a3f45"/><text x="${c.skWidth / 2}" y="${c.skHeight / 2 - 4}" class="lbl">0x${hex(w.addr)}</text><text x="${c.skWidth / 2}" y="${c.skHeight / 2 + 12}" class="lbl small">${t}</text>`, c.skWidth, c.skHeight);
    }
    const el = h('div', { class: 'vt-ws' + (w.addr === vt.active ? ' on' : ''), title: `Werkset 0x${hex(w.addr)} · ${w.state}`, html: inner });
    el.addEventListener('click', () => send({ t: 'vt.active', addr: w.addr }));
    return el;
  }));
}

function drawSoftKeys(ws) {
  const c = vt.cfg;
  const slots = [];
  let keys = [];
  const O = ws ? pools.get(ws.addr) : null;
  const skm = O && ws.softKeyMask !== NONE ? O[ws.softKeyMask] : null;
  if (skm?.type === 4) keys = skm.keys;
  const per = keys.length > c.skCount ? c.skCount - 1 : c.skCount;
  const pageKey = ws ? `${ws.addr}:${ws.softKeyMask}` : '';
  const pages = Math.max(1, Math.ceil(keys.length / per));
  let page = Math.min(skPage.get(pageKey) || 0, pages - 1);
  const shown = keys.slice(page * per, page * per + per);
  for (let i = 0; i < c.skCount; i++) {
    if (i === c.skCount - 1 && pages > 1) {
      const el = h('div', { class: 'vt-key nav', title: 'Volgende softkeys', html: svg(`<rect width="${c.skWidth}" height="${c.skHeight}" fill="#50565d"/><text x="${c.skWidth / 2}" y="${c.skHeight / 2 + 2}" class="lbl">▶ ${page + 1}/${pages}</text>`, c.skWidth, c.skHeight) });
      el.addEventListener('click', () => { skPage.set(pageKey, (page + 1) % pages); draw(); });
      slots.push(el);
      continue;
    }
    const key = shown[i] !== undefined ? keyOf(O, shown[i]) : null;
    if (!key) { slots.push(h('div', { class: 'vt-key empty' })); continue; }
    const el = h('div', { class: 'vt-key', 'data-key': key.id, html: svg(renderObject(P(ws.addr), key.id, c.skWidth, c.skHeight).markup, c.skWidth, c.skHeight) });
    pressable(el, ws.addr, key.id);
    slots.push(el);
  }
  skEl.replaceChildren(...slots);
}

/** Press / hold / release. The release is caught on the window: the key element may be redrawn meanwhile. */
let pressed = null; // { addr, id, pointerId }

function pressable(el, addr, id) {
  el.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    startPress(e, addr, id);
  });
}

function startPress(e, addr, id) {
  if (pressed) return;
  pressed = { addr, id, pointerId: e.pointerId };
  send({ t: 'vt.key', addr, id, action: 'press' });
  markPressed();
}

function endPress(e, action) {
  if (!pressed || (e && e.pointerId !== undefined && e.pointerId !== pressed.pointerId)) return;
  send({ t: 'vt.key', addr: pressed.addr, id: pressed.id, action });
  pressed = null;
  markPressed();
}
window.addEventListener('pointerup', (e) => endPress(e, 'release'));
window.addEventListener('pointercancel', (e) => endPress(e, 'abort'));
window.addEventListener('blur', () => endPress(null, 'abort'));

function markPressed() {
  for (const el of document.querySelectorAll('.vt-frame .down')) el.classList.remove('down');
  if (!pressed) return;
  for (const el of document.querySelectorAll(`.vt-key[data-key="${pressed.id}"], .vt-mask [data-btn="${pressed.id}"]`)) el.classList.add('down');
}

function maskDown(e) {
  if (e.button !== 0) return;
  const ws = activeWs();
  if (!ws || ws.state !== 'actief' || ws.locked) return;
  const O = pools.get(ws.addr);
  const btn = e.target.closest('[data-btn]');
  if (btn) {
    e.preventDefault();
    const id = Number(btn.dataset.btn);
    if (enabled(O[id])) startPress(e, ws.addr, id);
    return;
  }
  const inp = e.target.closest('[data-inp]');
  if (inp) {
    const id = Number(inp.dataset.inp);
    const o = O[id];
    if (!o || !enabled(o)) return;
    if (o.type === 7) {
      const v = valueOf(O, o);
      send({ t: 'vt.number', addr: ws.addr, id, value: v ? 0 : 1 });
    } else openInput(ws.addr, id, false);
    return;
  }
  const svgEl = maskEl.querySelector('svg');
  if (!svgEl) return;
  const pt = svgEl.createSVGPoint();
  pt.x = e.clientX;
  pt.y = e.clientY;
  const q = pt.matrixTransform(svgEl.getScreenCTM().inverse());
  send({ t: 'vt.pointing', addr: ws.addr, x: Math.round(q.x), y: Math.round(q.y), state: 1 });
  const up = () => { send({ t: 'vt.pointing', addr: ws.addr, x: Math.round(q.x), y: Math.round(q.y), state: 0 }); window.removeEventListener('pointerup', up); };
  window.addEventListener('pointerup', up);
}

const valueOf = (O, o) => {
  const v = O[o.variableReference];
  return v?.type === 21 ? v.value : o.value;
};

// ---------- input dialogs
function openInput(addr, id, fromImplement) {
  const O = pools.get(addr);
  const o = O?.[id];
  if (!o || ![8, 9, 10].includes(o.type)) return;
  closeDialog(false);
  if (!fromImplement) send({ t: 'vt.select', addr, id, open: true });
  const c = vt.cfg;
  const back = h('div', { class: 'modal-back' });
  const box = h('div', { class: 'modal' });
  back.append(box);
  dialog = { addr, id, back };
  const cancel = () => { send({ t: 'vt.esc', addr, id }); closeDialog(false); };
  back.addEventListener('pointerdown', (e) => { if (e.target === back) cancel(); });
  box.addEventListener('keydown', (e) => { if (e.key === 'Escape') cancel(); });
  const title = h('div', { class: 'modal-title' }, `${['', '', '', '', '', '', '', '', 'Tekst', 'Getal', 'Keuzelijst'][o.type]} · object ${id}`);
  if (o.type === 9) {
    const raw = valueOf(O, o) >>> 0;
    const disp = (r) => ((r >>> 0) + o.offset) * o.scale;
    const lo = Math.min(disp(o.minValue), disp(o.maxValue)), hi = Math.max(disp(o.minValue), disp(o.maxValue));
    const inp = h('input', { type: 'text', inputmode: 'decimal', class: 'big', value: formatNumber({ ...o, options: o.options & ~2 }, raw, 20) });
    const err = h('div', { class: 'err' });
    const ok = () => {
      const v = parseNum(inp.value);
      if (Number.isNaN(v)) { err.textContent = 'Geen getal'; return; }
      if (v < lo - 1e-9 || v > hi + 1e-9) { err.textContent = `Tussen ${fmt(lo, 10 ** -o.decimals)} en ${fmt(hi, 10 ** -o.decimals)}`; return; }
      const r = Math.round(v / o.scale - o.offset);
      send({ t: 'vt.number', addr, id, value: Math.min(o.maxValue, Math.max(o.minValue, r)) });
      closeDialog(false);
    };
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok(); });
    box.append(title, h('div', { class: 'muted' }, `Bereik ${fmt(lo, 10 ** -o.decimals)} … ${fmt(hi, 10 ** -o.decimals)}`), inp, err, keypad(inp),
      h('div', { class: 'modal-btns' }, h('button', { class: 'btn ghost', onclick: cancel }, 'Annuleren'), h('button', { class: 'btn primary', onclick: ok }, 'OK')));
    document.body.append(back);
    inp.focus();
    inp.select();
  } else if (o.type === 8) {
    const v = O[o.variableReference];
    const cur = v?.type === 22 ? v.value : o.value;
    const len = (v?.type === 22 ? v : o).length;
    const ia = O[o.inputAttributes];
    const inp = h('input', { type: 'text', class: 'big', maxlength: len, value: cur });
    if (ia?.type === 26 && ia.validationString) {
      const set = new Set(ia.validationString);
      inp.addEventListener('input', () => {
        const ok = [...inp.value].filter((ch) => (ia.validationType === 0 ? set.has(ch) : !set.has(ch))).join('');
        if (ok !== inp.value) inp.value = ok;
      });
    }
    const ok = () => { send({ t: 'vt.string', addr, id, value: inp.value }); closeDialog(false); };
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok(); });
    box.append(title, h('div', { class: 'muted' }, `Maximaal ${len} tekens`), inp,
      h('div', { class: 'modal-btns' }, h('button', { class: 'btn ghost', onclick: cancel }, 'Annuleren'), h('button', { class: 'btn primary', onclick: ok }, 'OK')));
    document.body.append(back);
    inp.focus();
    inp.select();
  } else {
    const [w, hh] = sizeOf(P(addr), o);
    const cur = valueOf(O, o);
    const items = (o.items || []).map((item, i) => {
      if (item === NONE || !O[item]) return null;
      const b = h('button', { class: 'listitem' + (i === cur ? ' on' : ''), html: svg(renderObject(P(addr), item, w, hh).markup, w, hh) });
      b.addEventListener('click', () => { send({ t: 'vt.number', addr, id, value: i }); closeDialog(false); });
      return b;
    });
    box.append(title, h('div', { class: 'list' }, items), h('div', { class: 'modal-btns' }, h('button', { class: 'btn ghost', onclick: cancel }, 'Annuleren')));
    document.body.append(back);
    box.tabIndex = -1;
    box.focus();
  }
}

function keypad(inp) {
  const keys = ['7', '8', '9', '4', '5', '6', '1', '2', '3', '-', '0', ',', '⌫'];
  return h('div', { class: 'keypad' }, keys.map((k) => h('button', { type: 'button', class: 'btn', onclick: () => {
    if (k === '⌫') inp.value = inp.value.slice(0, -1);
    else inp.value += k;
    inp.focus();
  } }, k)));
}

function closeDialog() {
  if (dialog) { dialog.back.remove(); dialog = null; }
}

function syncEdit() {
  if (dialog && !vt.ws.find((w) => w.addr === dialog.addr && w.state === 'actief')) closeDialog();
}

// ---------- side panel
function renderInfo(ws) {
  if (!ws) {
    infoEl.replaceChildren(h('span', { class: 'muted' }, `VT op adres ${vt.address != null ? '0x' + hex(vt.address) : '—'} (${vt.state}) · versie ${vt.cfg.version} · ${vt.cfg.dmSize}×${vt.cfg.dmSize} px`));
    return;
  }
  const O = pools.get(ws.addr) || {};
  const mask = O[ws.activeMask];
  infoEl.replaceChildren(...[
    h('b', null, ws.local ? `Bestand: ${ws.label}` : `Werkset 0x${hex(ws.addr)}`),
    h('span', { class: 'pill' + (mask?.type === 2 ? ' warn' : '') }, mask ? `${mask.type === 2 ? 'Alarmmasker' : 'Datamasker'} ${ws.activeMask}` : `masker ${ws.activeMask}`),
    h('span', { class: 'muted' }, `softkeys ${ws.softKeyMask === NONE ? '—' : ws.softKeyMask}`),
    ws.locked ? h('span', { class: 'pill warn' }, 'masker vergrendeld') : null,
    h('span', { class: 'grow' }),
    h('span', { class: 'muted' }, `VT 0x${hex(vt.address)} · v${vt.cfg.version}`)].filter(Boolean));
}

function renderList() {
  if (!vt.ws?.length) {
    listEl.replaceChildren(h('p', { class: 'muted' }, 'Nog geen werksets. Een werktuig verschijnt hier zodra het Working Set Maintenance of Get Memory naar de VT stuurt.'));
    return;
  }
  listEl.replaceChildren(...vt.ws.map((w) => {
    const pct = w.progress ? Math.round((100 * w.progress.got) / w.progress.size) : null;
    return h('div', { class: 'wsitem' + (w.addr === vt.active ? ' on' : '') },
      h('div', { class: 'wshead' },
        h('b', null, w.local ? `Bestand ${w.label}` : `0x${hex(w.addr)}`),
        h('span', { class: 'pill ' + ({ actief: 'ok', fout: 'err', uploaden: 'warn' }[w.state] || '') }, w.state + (pct !== null ? ` ${pct} %` : '')),
        h('span', { class: 'grow' }),
        w.state === 'actief' && w.addr !== vt.active ? h('button', { class: 'btn small', onclick: () => send({ t: 'vt.active', addr: w.addr }) }, 'Tonen') : null,
        w.poolSize ? h('a', { class: 'btn small ghost api-dl', href: `/api/pool/${w.addr}`, download: '' }, '.iop') : null,
        w.local ? h('button', { class: 'btn small ghost', onclick: () => send({ t: 'vt.close', addr: w.addr }) }, 'Sluiten') : null),
      w.local ? null : h('div', { class: 'muted mono small' }, `NAME ${w.name}`),
      h('div', { class: 'muted small' }, [
        w.vtVersion ? `vraagt VT v${w.vtVersion}` : null,
        w.poolSize ? `${w.objects} objecten, ${(w.poolSize / 1024).toFixed(1)} kB` : null,
        w.label && !w.local ? `versie "${w.label}"` : null,
      ].filter(Boolean).join(' · ')),
      w.error ? h('div', { class: 'err small' }, w.error) : null,
      w.warningCount ? h('details', { class: 'small' }, h('summary', null, `${w.warningCount} waarschuwing(en)`), h('ul', null, w.warnings.map((x) => h('li', null, x)))) : null);
  }));
}

function renderLog() {
  const rows = logRows.slice(-250).reverse();
  logEl.replaceChildren(h('table', { class: 'tbl log' }, h('tbody', null, rows.map((r) => h('tr', { class: r.dir },
    h('td', { class: 'mono muted' }, new Date(r.t * 1000).toLocaleTimeString('nl-BE', { hour12: false }) + '.' + String(Math.floor((r.t % 1) * 1000)).padStart(3, '0')),
    h('td', null, r.dir === 'in' ? '→ VT' : 'VT →'),
    h('td', { class: 'mono' }, r.addr === LOCAL ? '—' : '0x' + hex(r.addr)),
    h('td', null, r.func),
    h('td', { class: 'mono small' }, r.text))))));
}

// ---------- files & sound
function openFile(e) {
  const f = e.target.files[0];
  e.target.value = '';
  if (!f) return;
  const rd = new FileReader();
  rd.onload = () => {
    const bytes = new Uint8Array(rd.result);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    send({ t: 'vt.open', name: f.name, data: btoa(bin) });
    toast(`${f.name} geopend (${bytes.length} bytes)`);
  };
  rd.readAsArrayBuffer(f);
}

function audio(m) {
  if (m.volume !== undefined) { volume = m.volume / 100; return; }
  if (!audioCtx || !m.reps) return;
  let t = audioCtx.currentTime;
  for (let i = 0; i < Math.min(m.reps, 20); i++) {
    const osc = audioCtx.createOscillator();
    const g = audioCtx.createGain();
    osc.frequency.value = Math.max(50, Math.min(8000, m.freq || 1000));
    g.gain.value = 0.15 * volume;
    osc.connect(g).connect(audioCtx.destination);
    osc.start(t);
    osc.stop(t + (m.on || 100) / 1000);
    t += ((m.on || 100) + (m.off || 100)) / 1000;
  }
}
