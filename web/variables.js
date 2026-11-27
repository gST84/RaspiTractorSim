// Page "Variabelen": the CAN bus as an address space, browsed like an OPC UA client (UaExpert):
// a tree of devices, messages and signals (left), the attributes of the selected node (right), and a watch
// list with a trend (bottom). Frames come from a source: the simulator/server, a USB or serial CAN adapter
// directly in the browser, or a log file. Everything is decoded here, in the page; nothing is ever sent.

import { h, toast } from './util.js';
import { Model } from './can/model.js';
import { hex, hexBytes } from './can/frames.js';
import { position, fmtNum } from './can/signals.js';
import { LogSource, toCandump } from './can/sources/logfile.js';
import { ServerSource } from './can/sources/server.js';
import { UsbSource, usbAvailable } from './can/sources/usb.js';
import { SlcanSource, serialAvailable } from './can/sources/slcan.js';
import { parseDbc, loadStored, saveStored, deleteStored } from './can/dbc.js';
import { TcPlugin } from './can/tc.js';

export const model = new Model();
const tc = new TcPlugin();
model.plugins.push(tc);

const store = {
  get(k, d) { try { const v = localStorage.getItem(`tractorsim.var.${k}`); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(`tractorsim.var.${k}`, JSON.stringify(v)); } catch { /* private mode */ } },
};

/** Frame sources: { key, label, live, available(), options(): [elements], make(opts) → source }. */
export const SOURCES = [];
export function addSource(def) { SOURCES.push(def); }

const analyzerOnly = () => document.body.dataset.mode === 'analyzer';

addSource({ key: 'server', label: 'Simulator / server', live: true, available: () => !analyzerOnly(), make: () => new ServerSource() });

/** Bitrate choice for the adapters in the browser (stored per browser). */
const bitrate = () => Number(store.get('bitrate', 250000));
function bitrateOptions() {
  const sel = h('select', { class: 'sel', 'aria-label': 'Bitrate', title: 'Bitrate van de bus (ISOBUS: 250 kbit/s)' },
    [125000, 250000, 500000, 1000000].map((b) => h('option', { value: b }, `${b / 1000} kbit/s`)));
  sel.value = String(bitrate());
  sel.onchange = () => store.set('bitrate', Number(sel.value));
  return [sel];
}

addSource({
  key: 'usb', label: 'USB-adapter (WebUSB)', live: true, available: usbAvailable, options: bitrateOptions, bitrate,
  make: () => UsbSource.request(bitrate()),
});
addSource({
  key: 'serial', label: 'Seriële adapter (slcan)', live: true, available: serialAvailable, options: bitrateOptions, bitrate,
  make: () => SlcanSource.request(bitrate()),
});

let dbcBtn, root, treeEl, attrsEl, watchBody, trendCanvas, trendTip, statusEl, srcSel, optsEl, connBtn, pauseBtn, recBtn, searchIn, winSel, progressEl;
let source = null, sourceDef = null, srcMsg = '';
let paused = false;
let rec = null;
let visible = false, timer = null;
let selected = null; // node
let lastRev = -1, lastFilter = null, lastExpand = 0, tickN = 0, attrSig = '';
const expanded = new Set(store.get('expanded', ['bus']));
let expandRev = 0;
const rows = new Map(); // node id → row element
let hoverT = null;
const toolExtras = []; // buttons added by other modules (DBC, DDOP)

export function addTool(el) { toolExtras.push(el); root?.querySelector('.vtools')?.append(el); }

// ---------------------------------------------------------------- page
export function mount(el) {
  root = el;
  srcSel = h('select', { class: 'sel', 'aria-label': 'Bron', onchange: () => { store.set('source', srcSel.value); showOptions(); } });
  optsEl = h('span', { class: 'srcopts' });
  connBtn = h('button', { class: 'btn primary', onclick: () => (source ? disconnect() : connect()) }, 'Verbinden');
  pauseBtn = h('button', { class: 'btn ghost', title: 'Scherm bevriezen (afspelen pauzeren)', onclick: togglePause }, 'Pauze');
  recBtn = h('button', { class: 'btn ghost', title: 'Frames opnemen als candump-log', onclick: toggleRec }, 'Opnemen');
  dbcBtn = h('button', { class: 'btn ghost', title: 'DBC-bestanden voor eigen berichten', onclick: dbcDialog }, 'DBC');
  statusEl = h('span', { class: 'srcstatus muted' });
  progressEl = h('progress', { class: 'srcprog', max: 1, value: 0, hidden: true });
  searchIn = h('input', { type: 'search', placeholder: 'zoeken: naam, pad of PGN', oninput: () => { lastFilter = null; } });
  treeEl = h('div', { class: 'vtree', role: 'tree', tabindex: 0, 'aria-label': 'Adresruimte', onclick: treeClick, ondblclick: treeDbl, onkeydown: treeKey });
  attrsEl = h('div', { class: 'vattrs' });
  watchBody = h('tbody');
  trendCanvas = h('canvas', { class: 'vtrend', onmousemove: trendMove, onmouseleave: () => { hoverT = null; trendTip.hidden = true; } });
  trendTip = h('div', { class: 'vtip', hidden: true });
  winSel = h('select', { class: 'sel', onchange: () => store.set('window', Number(winSel.value)) },
    [[30, '30 s'], [120, '2 min'], [600, '10 min'], [0, 'alles']].map(([v, l]) => h('option', { value: v }, l)));
  winSel.value = String(store.get('window', 120));

  root.append(
    h('div', { class: 'vtoolbar card' },
      h('div', { class: 'vtools' }, srcSel, optsEl, connBtn, h('span', { class: 'sep' }), pauseBtn,
        h('button', { class: 'btn ghost', title: 'Alles wissen en opnieuw beginnen', onclick: clearAll }, 'Wissen'), recBtn, dbcBtn, ...toolExtras),
      h('div', { class: 'vstat' }, statusEl, progressEl)),
    h('div', { class: 'vgrid' },
      h('section', { class: 'card vtreecard' },
        h('h3', null, 'Adresruimte', h('span', { class: 'tools' }, searchIn)),
        treeEl),
      h('section', { class: 'card vattrcard' }, h('h3', null, 'Attributen'), attrsEl),
      h('section', { class: 'card vwatchcard' },
        h('h3', null, 'Volglijst',
          h('span', { class: 'tools' }, h('label', { class: 'muted small' }, 'Trend ', winSel),
            h('button', { class: 'btn small ghost', onclick: exportCsv }, 'CSV'),
            h('button', { class: 'btn small ghost', onclick: () => { for (const v of [...model.watch]) model.setWatch(v, false); } }, 'Leegmaken'))),
        h('div', { class: 'vtrendwrap' }, trendCanvas, trendTip),
        h('div', { class: 'tablewrap' }, h('table', { class: 'tbl vwatch' },
          h('thead', null, h('tr', null, ['Variabele', 'Waarde', '', 'Status', 'Tijd', 'Min', 'Max', ''].map((x) => h('th', null, x)))),
          watchBody)))));

  model.pendingWatch = new Set(store.get('watch', []));
  model.onWatch = () => { saveWatch(); renderWatch(true); };
  fillSources();
  initDbc();
}

// ---------------------------------------------------------------- DBC files
async function initDbc() {
  for (const e of await loadStored()) {
    try { model.dbcs.push({ name: e.name, enabled: e.enabled !== false, db: parseDbc(e.text), text: e.text }); } catch (err) { console.warn(e.name, err); }
  }
  if (model.dbcs.length) model.refreshDefs();
  updateDbcBtn();
}

function updateDbcBtn() {
  const on = model.dbcs.filter((d) => d.enabled).length;
  dbcBtn.textContent = on ? `DBC (${on})` : 'DBC';
}

function dbcDialog() {
  const back = h('div', { class: 'modal-back', onclick: (e) => { if (e.target === back) back.remove(); } });
  const list = h('div', { class: 'dbclist' });
  const changed = async (d) => {
    if (d) await saveStored({ name: d.name, text: d.text, enabled: d.enabled });
    model.refreshDefs();
    render();
    updateDbcBtn();
  };
  const render = () => list.replaceChildren(...(model.dbcs.length ? model.dbcs.map((d) => h('div', { class: 'dbcitem' },
    h('input', { type: 'checkbox', checked: d.enabled, title: 'Gebruiken', onchange: (e) => { d.enabled = e.target.checked; changed(d); } }),
    h('span', { class: 'grow' }, d.name),
    h('span', { class: 'muted small' }, `${d.db.messages.length} berichten${d.db.byPgn.size ? `, ${d.db.byPgn.size} op PGN` : ''}`),
    h('button', { class: 'btn small ghost', title: 'Verwijderen', onclick: async () => { model.dbcs = model.dbcs.filter((x) => x !== d); await deleteStored(d.name); changed(null); } }, '×')))
    : [h('p', { class: 'muted' }, 'Nog geen DBC geladen.')]));
  const add = h('button', { class: 'btn', onclick: async () => {
    const f = await pickFile('.dbc,.DBC');
    if (!f) return;
    try {
      const text = await f.text();
      const db = parseDbc(text);
      if (!db.messages.length) throw new Error('geen berichten (BO_) gevonden');
      model.dbcs = model.dbcs.filter((x) => x.name !== f.name);
      const d = { name: f.name, enabled: true, db, text };
      model.dbcs.push(d);
      await changed(d);
      toast(`${f.name}: ${db.messages.length} berichten${db.warnings.length ? ` (${db.warnings.length} waarschuwingen)` : ''}`);
    } catch (e) { toast(`DBC niet geladen: ${e.message}`, true); }
  } }, 'DBC toevoegen…');
  back.append(h('div', { class: 'modal', style: { width: 'min(540px, 100%)' }, role: 'dialog', 'aria-label': 'DBC-bestanden' },
    h('div', { class: 'modal-title' }, 'DBC-bestanden'),
    h('p', { class: 'muted small' }, 'Een DBC gaat voor op de ingebouwde berichten: eerst op het exacte CAN-ID, daarna op de PGN '
      + '(berichten met VFrameFormat J1939PG of ProtocolType J1939). De bestanden blijven in deze browser bewaard.'),
    list,
    h('div', { class: 'modal-btns' }, add, h('button', { class: 'btn primary', onclick: () => back.remove() }, 'Sluiten'))));
  render();
  document.body.append(back);
}

export function show() {
  visible = true;
  fillSources();
  if (!timer) timer = setInterval(tick, 100);
  lastRev = -1;
  tick();
}

export function hide() { visible = false; clearInterval(timer); timer = null; }

function fillSources() {
  const keep = srcSel.value || store.get('source', analyzerOnly() ? 'usb' : 'server');
  const list = SOURCES.filter((s) => s.available());
  srcSel.replaceChildren(...list.map((s) => h('option', { value: s.key }, s.label)));
  srcSel.value = list.some((s) => s.key === keep) ? keep : list[0]?.key;
  srcSel.disabled = !!source;
  showOptions();
}

function showOptions() {
  const def = SOURCES.find((s) => s.key === srcSel.value);
  optsEl.replaceChildren(...(def?.options?.() || []));
}

// ---------------------------------------------------------------- source handling
const sink = {
  frames(batch) {
    if (rec) {
      for (const f of batch) rec.lines.push(toCandump(f));
      if (rec.lines.length > 3e6) { toast('Opname gestopt: meer dan 3 miljoen frames', true); toggleRec(); }
    }
    model.push(batch);
  },
  status(text) { srcMsg = text; showStatus(); },
  progress(p) { progressEl.hidden = p >= 1; progressEl.value = p; },
  lost(n) { model.stats.lost += n; },
  closed(err, text) {
    if (err) toast(err, true);
    srcMsg = text || err || 'gestopt';
    showStatus();
    progressEl.hidden = true;
    if (sourceDef && !sourceDef.live) { source = null; setConnected(false); } // a file that ended
    else if (err) { source = null; setConnected(false); }
  },
};

async function connect() {
  const def = SOURCES.find((s) => s.key === srcSel.value);
  if (!def) return;
  let src;
  try {
    src = await def.make();
    if (!src) return; // cancelled (no file or device chosen)
  } catch (e) {
    if (e?.name !== 'NotFoundError') toast(`Bron niet geopend: ${e.message || e}`, true);
    return;
  }
  source = src;
  sourceDef = def;
  if (def.bitrate) model.stats.bitrate = def.bitrate();
  setConnected(true);
  try {
    await src.open(sink);
  } catch (e) {
    toast(`Verbinden mislukt: ${e.message || e}`, true);
    srcMsg = ''; showStatus();
    source = null;
    setConnected(false);
  }
}

async function disconnect() {
  const s = source;
  source = null;
  setConnected(false);
  try { await s?.close(); } catch (e) { console.warn(e); }
  srcMsg = 'verbinding verbroken'; showStatus();
}

function setConnected(on) {
  connBtn.textContent = on ? (sourceDef?.live ? 'Verbreken' : 'Stoppen') : 'Verbinden';
  connBtn.classList.toggle('primary', !on);
  srcSel.disabled = on;
  for (const x of optsEl.querySelectorAll('select,input,button')) x.disabled = on;
}

function togglePause() {
  paused = !paused;
  pauseBtn.textContent = paused ? 'Verder' : 'Pauze';
  pauseBtn.classList.toggle('on', paused);
  source?.setPaused?.(paused);
  if (!paused) { lastRev = -1; tick(); }
}

function clearAll() {
  model.reset();
  selected = null;
  rows.clear();
  treeEl.replaceChildren();
  lastRev = -1;
  renderWatch(true);
  tick();
}

function toggleRec() {
  if (!rec) {
    rec = { lines: [], start: new Date() };
    recBtn.textContent = 'Stop opname';
    recBtn.classList.add('rec');
    return;
  }
  const r = rec;
  rec = null;
  recBtn.textContent = 'Opnemen';
  recBtn.classList.remove('rec');
  if (!r.lines.length) { toast('Geen frames opgenomen'); return; }
  const p = (n) => String(n).padStart(2, '0');
  const d = r.start;
  download(`canlog-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.log`,
    r.lines.join('\n') + '\n', 'text/plain');
}

export function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = h('a', { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

/** The log file source: a file input and a play mode. */
export function logOptions() {
  const mode = h('select', { class: 'sel', 'aria-label': 'Afspelen' },
    h('option', { value: 'load' }, 'in één keer'), h('option', { value: '1' }, 'afspelen ×1'),
    h('option', { value: '5' }, 'afspelen ×5'), h('option', { value: '20' }, 'afspelen ×20'));
  mode.value = store.get('logmode', 'load');
  mode.onchange = () => store.set('logmode', mode.value);
  return [mode];
}

function pickFile(accept) {
  return new Promise((resolve) => {
    const inp = h('input', { type: 'file', accept, style: { display: 'none' } });
    inp.onchange = () => { resolve(inp.files[0] || null); inp.remove(); };
    inp.oncancel = () => { resolve(null); inp.remove(); };
    document.body.append(inp);
    inp.click();
  });
}

addSource({
  key: 'log', label: 'Logbestand', live: false, available: () => true, options: logOptions,
  async make() {
    const file = await pickFile('.can,.CAN,.log,.trc,.csv,.txt,.asc');
    if (!file) return null;
    const mode = store.get('logmode', 'load');
    model.reset();
    rows.clear();
    treeEl.replaceChildren();
    return new LogSource(file.name, await file.text(), mode === 'load' ? 'load' : 'play', mode === 'load' ? 1 : Number(mode));
  },
});

// ---------------------------------------------------------------- tree
const isLive = () => !!(source && sourceDef?.live);
const nowT = () => (isLive() ? Date.now() / 1000 : model.now);

function varState(v, now) {
  if (v.status === 'none') return 'none';
  if (v.status !== 'ok') return v.status;
  const p = v.parent;
  if (p.kind === 'msg' && msgStale(p, now)) return 'stale';
  return 'ok';
}

/** A cyclic message (seen at least 3 times) that stopped for 3 periods (and at least 2,5 s). */
const msgStale = (m, now) => isLive() && m.count >= 3 && m.period > 0 && now - m.t > Math.max(3 * m.period, 2.5);

const fmtPeriod = (p) => (!p ? '' : p < 0.995 ? `${Math.round(p * 1000)} ms` : `${p.toFixed(p < 9.95 ? 1 : 0).replace('.', ',')} s`);

const STATUS_TEXT = { ok: 'goed', na: 'n.b.', err: 'fout', short: 'te kort', stale: 'verouderd', none: 'geen waarde' };

function visibleNodes() {
  const q = searchIn.value.trim().toLowerCase();
  const out = [];
  if (q) {
    for (const n of model.walkAll()) {
      if (n.kind !== 'var' && n.kind !== 'msg') continue;
      const hay = `${n.path} ${n.kind === 'msg' ? hex(n.pgn ?? 0, 4) + ' ' + (n.pgn ?? '') : ''}`.toLowerCase();
      if (hay.includes(q)) out.push({ node: n, depth: 0, flat: true });
      if (out.length >= 400) break;
    }
    return out;
  }
  const walk = (node, depth) => {
    out.push({ node, depth });
    if (expanded.has(node.id)) for (const k of node.kids()) walk(k, depth + 1);
  };
  walk(model.root, 0);
  return out;
}

function makeRow(n) {
  const p = {
    tw: h('span', { class: 'tw' }), ic: h('span', { class: `ic k-${n.kind}` }), nm: h('span', { class: 'nm' }),
    vl: h('span', { class: 'vl' }), un: h('span', { class: 'un' }),
    add: n.kind === 'var' ? h('button', { class: 'add', title: 'Volgen', tabindex: -1 }, '+') : h('span', { class: 'add' }),
  };
  const row = h('div', { class: `tr k-${n.kind}`, role: 'treeitem', 'data-id': n.id }, p.tw, p.ic, p.nm, p.vl, p.un, p.add);
  row._p = p;
  row._n = n;
  return row;
}

function setText(el, text) { if (el.textContent !== text) el.textContent = text; }

function updateRow(row, n, depth, flat, now) {
  const p = row._p;
  row._n = n;
  row.style.setProperty('--d', depth);
  const leaf = n.kind === 'var';
  setText(p.tw, leaf || flat ? '' : expanded.has(n.id) ? '▾' : '▸');
  setText(p.nm, flat ? n.path : n.kind === 'msg' ? `${n.key} · ${n.label}` : n.label);
  row.classList.toggle('sel', selected === n);
  if (leaf) {
    const st = varState(n, now);
    setText(p.vl, n.text);
    setText(p.un, n.unit);
    row.dataset.st = st;
    const w = model.watch.has(n);
    if (p.add.classList.contains('on') !== w) { p.add.classList.toggle('on', w); p.add.textContent = w ? '★' : '+'; p.add.title = w ? 'Niet meer volgen' : 'Volgen'; }
    row.setAttribute('aria-selected', selected === n ? 'true' : 'false');
  } else if (n.kind === 'msg') {
    setText(p.vl, `${n.count.toLocaleString('nl-BE')}×`);
    setText(p.un, n.count >= 3 ? fmtPeriod(n.period) : '');
    row.dataset.st = msgStale(n, now) ? 'stale' : 'ok';
  } else if (n.kind === 'device') {
    setText(p.vl, n.sub);
    setText(p.un, '');
    row.dataset.st = n.lost ? 'stale' : 'ok';
  } else if (n.kind === 'group') {
    setText(p.vl, String(n.kids().length));
    setText(p.un, '');
  } else if (n.kind === 'bus') {
    setText(p.vl, `${model.deviceList().length} toestellen`);
    setText(p.un, '');
  }
}

function renderTree() {
  const now = nowT();
  const list = visibleNodes();
  const structural = model.rev !== lastRev || searchIn.value !== lastFilter || expandRev !== lastExpand;
  const els = [];
  for (const { node, depth, flat } of list) {
    let row = rows.get(node.id);
    if (!row || row._n !== node) { row = makeRow(node); rows.set(node.id, row); }
    updateRow(row, node, depth, flat, now);
    els.push(row);
  }
  if (structural) {
    treeEl.replaceChildren(...els);
    if (rows.size > els.length * 3 + 500) { // forget rows of collapsed branches
      const keep = new Set(els);
      for (const [id, r] of rows) if (!keep.has(r)) rows.delete(id);
    }
    lastRev = model.rev; lastFilter = searchIn.value; lastExpand = expandRev;
  }
}

function toggle(n, open = !expanded.has(n.id)) {
  if (n.kind === 'var') return;
  if (open) expanded.add(n.id); else expanded.delete(n.id);
  expandRev++;
  store.set('expanded', [...expanded].filter((id) => !id.includes('/') || id.split('/').length < 3).slice(0, 200));
  renderTree();
}

function select(n) {
  selected = n;
  attrSig = '';
  renderTree();
  renderAttrs();
  const row = rows.get(n.id);
  row?.scrollIntoView({ block: 'nearest' });
}

/** Opens the branches above a node (for a click in the watch list). */
function reveal(n) {
  for (let p = n.parent; p; p = p.parent) expanded.add(p.id);
  expanded.add('bus');
  expandRev++;
  searchIn.value = '';
  select(n);
}

function treeClick(e) {
  const row = e.target.closest('.tr');
  if (!row) return;
  const n = row._n;
  if (e.target.closest('.add') && n.kind === 'var') { model.setWatch(n, !model.watch.has(n)); renderTree(); return; }
  if (e.target.closest('.tw')) { toggle(n); return; }
  select(n);
}

function treeDbl(e) {
  const row = e.target.closest('.tr');
  if (!row) return;
  const n = row._n;
  if (n.kind === 'var') model.setWatch(n, !model.watch.has(n));
  else toggle(n);
}

function treeKey(e) {
  const list = [...treeEl.children];
  if (!list.length) return;
  let i = selected ? list.findIndex((r) => r._n === selected) : -1;
  const n = selected;
  switch (e.key) {
    case 'ArrowDown': i = Math.min(list.length - 1, i + 1); break;
    case 'ArrowUp': i = Math.max(0, i - 1); break;
    case 'ArrowRight': if (n && n.kind !== 'var' && !expanded.has(n.id)) { toggle(n, true); e.preventDefault(); return; } i = Math.min(list.length - 1, i + 1); break;
    case 'ArrowLeft':
      if (n && expanded.has(n.id)) { toggle(n, false); e.preventDefault(); return; }
      if (n?.parent) { const pr = rows.get(n.parent.id); if (pr) { select(n.parent); e.preventDefault(); } }
      return;
    case 'Enter': case ' ': if (n?.kind === 'var') model.setWatch(n, !model.watch.has(n)); else if (n) toggle(n); e.preventDefault(); return;
    default: return;
  }
  e.preventDefault();
  select(list[Math.max(0, i)]._n);
}

// ---------------------------------------------------------------- attributes
const fmtTime = (t) => {
  if (!t) return '—';
  if (t > 1e9) {
    const d = new Date(t * 1000);
    return d.toLocaleTimeString('nl-BE') + ',' + String(d.getMilliseconds()).padStart(3, '0');
  }
  return `${t.toFixed(3)} s`;
};

function srcText(src) {
  if (!src) return '';
  if (src === 'iso') return 'ingebouwd (ISO 11783 / J1939 / NMEA 2000)';
  if (src === 'raw') return 'geen definitie';
  if (src.startsWith('dbc:')) return `DBC ${src.slice(4)}`;
  return src;
}

function attrRows(n) {
  const now = nowT();
  const R = [];
  const add = (k, v, cls) => { if (v !== undefined && v !== null && v !== '') R.push([k, v, cls]); };
  if (n.kind === 'var') {
    const s = n.def || {};
    const st = varState(n, now);
    add('Pad', n.path, 'mono');
    add('Waarde', `${n.text}${n.unit ? ' ' + n.unit : ''}`, `big st-${st}`);
    add('Status', STATUS_TEXT[st] ?? st);
    if (n.raw !== null && n.raw !== undefined && typeof n.value !== 'string') add('Ruwe waarde', `${n.raw} (0x${(typeof n.raw === 'bigint' ? n.raw : BigInt(Math.trunc(Number(n.raw)))).toString(16).toUpperCase()})`, 'mono');
    add('Tijdstempel', fmtTime(n.t));
    if (n.t && isLive()) add('Leeftijd', `${Math.max(0, now - n.t).toFixed(1)} s`);
    if (n.min !== null) add('Min / max', `${fmtNum(n.min, s)} / ${fmtNum(n.max, s)} ${n.unit}`);
    add('Updates', n.n.toLocaleString('nl-BE'));
    if (s.start !== undefined) {
      add('Positie', position(s));
      add('Datatype', `${s.type === 'text' ? 'tekst' : s.type === 'float' ? 'float' + s.len : (s.signed ? 'int' : 'uint') + s.len}${s.order === 'be' ? ', Motorola' : ''}`);
      if (s.scale !== undefined && s.type !== 'text' && s.type !== 'enum') add('Schaal / offset', `${s.scale} / ${s.offset ?? 0}`);
      if (s.na) add('Niet beschikbaar', s.na === 'j1939' ? 'J1939-bereiken (FB…FF = n.b., FE = fout)' : 'NMEA 2000 (alle bits 1)');
      if (s.values) add('Keuzelijst', Object.entries(s.values).map(([k, v]) => `${k} = ${v}`).join(' · '));
      if (s.mux) add('Multiplex', `${s.mux.by} = ${s.mux.value}`);
      if (s.comment) add('Commentaar', s.comment);
      if (s.note || n.parent.def?.note) add('Opmerking', s.note || n.parent.def.note, 'warn');
    }
    if (s.ddi !== undefined) add('DDI', `${s.ddi}${s.ddiName ? ' · ' + s.ddiName : ''}`);
    if (s.source) add('Bron', s.source);
    if (n.parent.kind === 'msg') {
      add('Bericht', `${n.parent.name}${n.parent.pgn !== null ? ` (PGN ${n.parent.pgn} / 0x${hex(n.parent.pgn, 4)})` : ''}`);
      add('Bron', srcText(n.parent.src));
    }
  } else if (n.kind === 'msg') {
    add('Pad', n.path, 'mono');
    add('Naam', n.name);
    if (n.pgn !== null) add('PGN', `${n.pgn} (0x${hex(n.pgn, 4)})`);
    if (n.canId !== null) add('CAN-ID', `0x${hex(n.canId, n.parent.sa === null ? 3 : 8)}`, 'mono');
    if (n.parent.sa !== null) add('Bron → bestemming', `${hex(n.parent.sa)} → ${n.da === 0xff || n.da === null ? 'alle (FF)' : hex(n.da)}`);
    add('Aantal', n.count.toLocaleString('nl-BE'));
    if (n.period) add('Periode', `${(n.period * 1000).toFixed(1)} ms`);
    add('Laatst', fmtTime(n.t));
    if (n.via) add('Transport', { TP: 'TP (≤ 1785 byte)', ETP: 'ETP (> 1785 byte)', FP: 'NMEA 2000 fast packet' }[n.via] || n.via);
    if (n.data) add('Data', `${n.data.length} byte: ${hexBytes(n.data.subarray(0, 64))}${n.data.length > 64 ? ' …' : ''}`, 'mono');
    add('Definitie', srcText(n.src));
    if (n.def?.comment) add('Commentaar', n.def.comment);
    if (n.def?.sender) add('Zender (DBC)', n.def.sender);
    add('Signalen', String(n.vars.length));
    if (n.def?.note) add('Opmerking', n.def.note, 'warn');
  } else if (n.kind === 'device') {
    add('Adres', n.sa === null ? '11-bit identifiers' : `${hex(n.sa)} (${n.sa})${n.lost ? ' · adres overgenomen door een ander toestel' : ''}`);
    if (n.nameHex) {
      add('NAME', n.nameHex, 'mono');
      add('Functie', n.ident.items.get('Functie')?.text);
      add('Fabrikant', n.ident.items.get('Fabrikant')?.text);
      add('Software', n.ident.items.get('Software')?.text);
    } else if (n.sa !== null) add('NAME', 'nog geen Address Claimed gezien (die komt enkel bij het opstarten of op verzoek)');
    add('Frames', n.frames.toLocaleString('nl-BE'));
    add('Berichten', String(n.msgs.size));
    if (n.tc?.objects) add('DDOP', `${n.tc.objects.length} objecten (${n.tc.src === 'bus' ? 'opgevangen op de bus' : n.tc.src})`);
    else if (n.tc?.error) add('DDOP', `niet leesbaar: ${n.tc.error}`, 'warn');
    add('Eerst gezien', fmtTime(n.firstT));
    add('Laatst gezien', fmtTime(n.lastT));
  } else if (n.kind === 'group') {
    add('Pad', n.path, 'mono');
    add('Onderdelen', String(n.kids().length));
  } else if (n.kind === 'bus') {
    const s = model.stats;
    add('Bron', source ? `${source.label}` : 'niet verbonden');
    add('Bitrate', `${s.bitrate / 1000} kbit/s`);
    add('Frames', `${s.frames.toLocaleString('nl-BE')} (${s.ext.toLocaleString('nl-BE')} × 29 bit, ${s.std.toLocaleString('nl-BE')} × 11 bit)`);
    add('Frames per seconde', String(s.rate));
    add('Busbelasting', `${s.load.toFixed(1)} % (schatting)`);
    if (s.lost) add('Verloren', `${s.lost.toLocaleString('nl-BE')} frames (de bron kon niet volgen)`, 'warn');
    add('Toestellen', String(model.deviceList().length));
    const tr = model.transport;
    add('Transport', `${tr.stats.tp} TP, ${tr.stats.etp} ETP, ${tr.stats.fast} fast packet afgewerkt${tr.stats.aborted ? `, ${tr.stats.aborted} afgebroken` : ''}`);
    const act = tr.active();
    if (act.length) add('Lopend', act.map((a) => `${a.kind.toUpperCase()} ${hex(a.sa)}→${hex(a.da)} PGN ${hex(a.pgn, 4)} ${a.done}/${a.size}`).join(' · '));
  }
  return R;
}

function renderAttrs() {
  if (!selected) {
    if (attrSig !== 'none') {
      attrsEl.replaceChildren(h('p', { class: 'muted' }, 'Kies een knooppunt in de boom. Dubbelklik (of + ) op een variabele om ze te volgen.'));
      attrSig = 'none';
    }
    return;
  }
  const R = attrRows(selected);
  const sig = JSON.stringify(R);
  if (sig === attrSig) return;
  attrSig = sig;
  const n = selected;
  const extra = [];
  if (n.kind === 'var') {
    const w = model.watch.has(n);
    extra.push(h('button', { class: 'btn small', onclick: () => { model.setWatch(n, !model.watch.has(n)); attrSig = ''; renderAttrs(); } }, w ? 'Niet meer volgen' : 'Volgen'));
  } else if (n.kind === 'msg' && n.vars.length) {
    extra.push(h('button', { class: 'btn small', onclick: () => { for (const v of n.vars) if (typeof v.value === 'number' || v.value === null) model.setWatch(v, true); } }, 'Alle signalen volgen'));
  }
  for (const fn of attrExtras) { const el = fn(n); if (el) extra.push(...[el].flat().filter(Boolean)); }
  attrsEl.replaceChildren(
    h('div', { class: 'vattrhead' }, h('span', { class: `ic k-${n.kind}` }), h('b', null, n.label)),
    h('dl', null, R.map(([k, v, cls]) => [h('dt', null, k), h('dd', { class: cls || '' }, v)])),
    extra.length ? h('div', { class: 'toolbar small' }, extra) : null);
}

const attrExtras = [];
/** fn(node) → element or null: extra actions in the attributes panel (DDOP, …). */
export function addAttrAction(fn) { attrExtras.push(fn); }

// DDOP of an implement: load one from a file (when the transfer was not on the log) or save the caught one
addAttrAction((n) => {
  if (n.kind !== 'device' || n.sa === null) return null;
  const st = n.tc;
  return [
    h('button', { class: 'btn small', title: 'Binaire DDOP (zoals het werktuig hem naar de TC stuurt)', onclick: async () => {
      const f = await pickFile('.bin,.ddop,.iop,.dat,application/octet-stream');
      if (!f) return;
      const ok = tc.setDdop(model, n, new Uint8Array(await f.arrayBuffer()), f.name, nowT());
      toast(ok ? `DDOP ${f.name} geladen` : `DDOP niet leesbaar: ${n.tc?.error}`, !ok);
      attrSig = '';
    } }, 'DDOP laden…'),
    st?.bytes ? h('button', { class: 'btn small ghost', onclick: () => {
      const url = URL.createObjectURL(new Blob([st.bytes], { type: 'application/octet-stream' }));
      const a = h('a', { href: url, download: `ddop_${hex(n.sa)}.bin` });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } }, 'DDOP bewaren') : null,
  ];
});

// ---------------------------------------------------------------- watch list
let watchSig = '';
function saveWatch() { store.set('watch', [...model.watch].map((v) => v.stableKey).concat([...model.pendingWatch]).slice(0, 100)); }

function renderWatch(force) {
  const list = [...model.watch];
  const sig = list.map((v) => v.id).join('|');
  const now = nowT();
  if (force || sig !== watchSig) {
    watchSig = sig;
    watchBody.replaceChildren(...list.map((v) => {
      const tr = h('tr', { onclick: (e) => { if (!e.target.closest('button')) reveal(v); } },
        h('td', { class: 'mono path' }, v.path), h('td', { class: 'r val' }), h('td', { class: 'muted' }, v.unit), h('td', { class: 'stc' }),
        h('td', { class: 'muted tm' }), h('td', { class: 'r mn' }), h('td', { class: 'r mx' }),
        h('td', null, h('button', { class: 'btn small ghost', title: 'Niet meer volgen', onclick: () => model.setWatch(v, false) }, '×')));
      tr._v = v;
      return tr;
    }));
    if (!list.length) watchBody.append(h('tr', null, h('td', { colspan: 8, class: 'muted' }, model.pendingWatch.size
      ? `${model.pendingWatch.size} bewaarde variabele(n) wachten op hun eerste waarde.` : 'Nog niets gevolgd: kies + bij een variabele in de boom.')));
  }
  for (const tr of watchBody.children) {
    const v = tr._v;
    if (!v) continue;
    const st = varState(v, now);
    const c = tr.children;
    setText(c[1], v.text);
    c[1].dataset.st = st;
    setText(c[3], STATUS_TEXT[st] ?? st);
    c[3].dataset.st = st;
    setText(c[4], fmtTime(v.t));
    setText(c[5], v.min === null ? '' : fmtNum(v.min, v.def));
    setText(c[6], v.max === null ? '' : fmtNum(v.max, v.def));
  }
}

function exportCsv() {
  const list = [...model.watch].filter((v) => v.hist && v.hist.n);
  if (!list.length) { toast('Geen gevolgde waarden om te exporteren'); return; }
  const lines = ['variabele;eenheid;tijd;waarde'];
  for (const v of list) {
    const s = v.series();
    for (let i = 0; i < s.t.length; i++) lines.push(`${v.path};${v.unit};${s.t[i].toFixed(4)};${String(s.v[i]).replace('.', ',')}`);
  }
  download('volglijst.csv', lines.join('\n') + '\n', 'text/csv');
}

// ---------------------------------------------------------------- trend: one small chart per numeric variable
const PANEL = 78, AXIS = 22, LEFT = 64, RIGHT = 74;

function trendVars() { return [...model.watch].filter((v) => v.hist && v.hist.n); }

/** Round step for an axis: 1, 2 or 5 × 10^n. */
function niceStep(x) {
  const e = 10 ** Math.floor(Math.log10(x));
  const f = x / e;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * e;
}

function drawTrend() {
  const vars = trendVars();
  const wrap = trendCanvas.parentElement;
  wrap.hidden = !vars.length;
  if (!vars.length) return;
  const css = getComputedStyle(root);
  const ink = css.getPropertyValue('--muted').trim() || '#677068';
  const grid = css.getPropertyValue('--line').trim() || '#dde1dc';
  const lineC = css.getPropertyValue('--trend').trim() || '#1f6feb';
  const surface = css.getPropertyValue('--panel').trim() || '#fff';
  const textC = css.getPropertyValue('--ink').trim() || '#1c221d';
  const W = wrap.clientWidth;
  const H = vars.length * PANEL + AXIS;
  const dpr = window.devicePixelRatio || 1;
  if (trendCanvas.width !== Math.round(W * dpr) || trendCanvas.height !== Math.round(H * dpr)) {
    trendCanvas.width = Math.round(W * dpr); trendCanvas.height = Math.round(H * dpr);
    trendCanvas.style.height = `${H}px`;
  }
  const g = trendCanvas.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  const series = vars.map((v) => v.series());
  const now = nowT();
  const win = Number(winSel.value);
  let t1 = now, t0;
  if (win) t0 = t1 - win;
  else { t0 = Math.min(...series.map((s) => s.t[0] ?? now)); if (t1 - t0 < 1) t0 = t1 - 1; }
  const x = (t) => LEFT + ((t - t0) / (t1 - t0)) * (W - LEFT - RIGHT);
  g.font = '11px system-ui, sans-serif';
  trendCanvas._geo = { t0, t1, W, vars, series };
  const ab = vars.length * PANEL;
  // time grid first (hairlines), the data on top
  const span = t1 - t0;
  const stepT = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200].find((st) => span / st <= 6) || 3600 * 4;
  g.strokeStyle = grid; g.lineWidth = 1;
  g.fillStyle = ink; g.textBaseline = 'top'; g.textAlign = 'center';
  for (let t = Math.ceil(t0 / stepT) * stepT; t <= t1; t += stepT) {
    const px = Math.round(x(t)) + 0.5;
    g.beginPath(); g.moveTo(px, 0); g.lineTo(px, ab - 8); g.stroke();
    g.fillText(t > 1e9 ? new Date(t * 1000).toLocaleTimeString('nl-BE') : `${Math.round(t)} s`, px, ab - 2);
  }
  vars.forEach((v, i) => {
    const top = i * PANEL + 18, bot = (i + 1) * PANEL - 8;
    const s = series[i];
    let lo = Infinity, hi = -Infinity;
    for (let k = 0; k < s.t.length; k++) if (s.t[k] >= t0) { lo = Math.min(lo, s.v[k]); hi = Math.max(hi, s.v[k]); }
    if (!Number.isFinite(lo)) { lo = v.value ?? 0; hi = lo; }
    if (hi === lo) { const d = Math.max(Math.abs(hi) * 0.05, v.def?.scale || 1); hi += d; lo -= d; }
    const st = niceStep((hi - lo) / 2);
    lo = Math.floor(lo / st) * st; hi = Math.ceil(hi / st) * st;
    const y = (val) => bot - ((val - lo) / (hi - lo)) * (bot - top);
    g.strokeStyle = grid; g.lineWidth = 1;
    g.beginPath(); g.moveTo(LEFT, Math.round(bot) + 0.5); g.lineTo(W - RIGHT, Math.round(bot) + 0.5);
    g.moveTo(LEFT, Math.round(top) + 0.5); g.lineTo(W - RIGHT, Math.round(top) + 0.5); g.stroke();
    const axisFmt = { scale: st };
    g.fillStyle = ink; g.textAlign = 'right'; g.textBaseline = 'middle';
    g.fillText(fmtNum(hi, axisFmt), LEFT - 6, top);
    g.fillText(fmtNum(lo, axisFmt), LEFT - 6, bot);
    // title above the panel, on the surface (not over the grid)
    const title = `${v.path}${v.unit ? ' [' + v.unit + ']' : ''}`;
    g.textAlign = 'left'; g.textBaseline = 'top';
    g.fillStyle = surface; g.fillRect(LEFT, top - 16, Math.min(g.measureText(title).width + 8, W - LEFT - RIGHT), 14);
    g.fillStyle = textC; g.fillText(title, LEFT + 2, top - 15);
    // the line: 2 px, round joins, steps for enumerations
    const step = v.def?.type === 'enum' || !!v.def?.values;
    g.strokeStyle = lineC; g.lineWidth = 2; g.lineJoin = 'round'; g.lineCap = 'round';
    g.save(); g.beginPath(); g.rect(LEFT, top - 2, W - LEFT - RIGHT, bot - top + 4); g.clip();
    g.beginPath();
    let started = false, py = 0, lastX = -1, lastV = null;
    for (let k = 0; k < s.t.length; k++) {
      if (s.t[k] < t0 && k + 1 < s.t.length && s.t[k + 1] < t0) continue;
      const px = x(s.t[k]), yy = y(s.v[k]);
      if (!started) { g.moveTo(px, yy); started = true; }
      else if (step) { g.lineTo(px, py); g.lineTo(px, yy); }
      else if (Math.abs(px - lastX) >= 0.5 || k === s.t.length - 1) g.lineTo(px, yy);
      py = yy; lastX = px; lastV = s.v[k];
    }
    g.stroke(); g.restore();
    // end dot (r 4 with a 2 px surface ring) and the last value beside it, in text colour
    if (lastV !== null) {
      const ex = Math.min(x(s.t[s.t.length - 1]), W - RIGHT), ey = y(lastV);
      g.beginPath(); g.arc(ex, ey, 6, 0, 2 * Math.PI); g.fillStyle = surface; g.fill();
      g.beginPath(); g.arc(ex, ey, 4, 0, 2 * Math.PI); g.fillStyle = lineC; g.fill();
      g.fillStyle = textC; g.textAlign = 'left'; g.textBaseline = 'middle';
      g.fillText(fmtNum(lastV, v.def), W - RIGHT + 10, Math.max(top + 4, Math.min(bot - 4, ey)));
    }
  });
  // crosshair
  if (hoverT !== null && hoverT >= t0 && hoverT <= t1) {
    const px = Math.round(x(hoverT)) + 0.5;
    g.strokeStyle = ink; g.lineWidth = 1; g.beginPath(); g.moveTo(px, 0); g.lineTo(px, ab - 8); g.stroke();
  }
}

function valueAt(s, t) {
  let best = null;
  for (let k = 0; k < s.t.length; k++) { if (s.t[k] <= t) best = k; else break; }
  return best === null ? null : { t: s.t[best], v: s.v[best] };
}

function trendMove(e) {
  const geo = trendCanvas._geo;
  if (!geo) return;
  const r = trendCanvas.getBoundingClientRect();
  const px = e.clientX - r.left;
  if (px < LEFT || px > geo.W - RIGHT) { hoverT = null; trendTip.hidden = true; drawTrend(); return; }
  hoverT = geo.t0 + ((px - LEFT) / (geo.W - LEFT - RIGHT)) * (geo.t1 - geo.t0);
  trendTip.hidden = false;
  trendTip.replaceChildren(h('div', { class: 'muted' }, fmtTime(hoverT)),
    ...geo.vars.map((v, i) => { const a = valueAt(geo.series[i], hoverT); return h('div', null, h('span', { class: 'muted' }, `${v.name}: `), a ? `${fmtNum(a.v, v.def)} ${v.unit}` : '—'); }));
  const left = Math.min(px + 14, geo.W - 200);
  trendTip.style.left = `${Math.max(0, left)}px`;
  trendTip.style.top = `${Math.max(0, e.clientY - r.top - 20)}px`;
  drawTrend();
}

// ---------------------------------------------------------------- refresh
function showStatus() {
  const s = model.stats;
  const parts = [srcMsg || (source ? source.label : 'Kies een bron en verbind.')];
  if (s.frames) parts.push(`${s.frames.toLocaleString('nl-BE')} frames`, `${model.deviceList().length} toestellen`);
  if (source && isLive()) parts.push(`${s.rate} fr/s`, `${s.load.toFixed(1)} %`);
  if (s.lost) parts.push(`${s.lost.toLocaleString('nl-BE')} verloren`);
  setText(statusEl, parts.join(' · '));
}

function tick() {
  tickN++;
  if (isLive() && tickN % 10 === 0) model.rates(Date.now() / 1000);
  if (!visible || paused) return;
  renderTree();
  if (tickN % 3 === 0 || attrSig === '') renderAttrs();
  renderWatch(false);
  drawTrend();
  if (tickN % 5 === 0) showStatus();
}
