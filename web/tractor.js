// Tractor page: every simulated value as a control, live tiles, the messages that are sent and
// what implements send to the tractor.
import { h, $, fmt, hex, parseNum, send, on, throttle, pgnName } from './util.js';

const ORDER = ['Motor', 'Rijden', 'Achterhef', 'Aftakas achter', 'Fronthef', 'Aftakas voor', 'Hydrauliek', 'Verlichting', 'GNSS', 'Systeem', "Werktuigcommando's"];
let P = [], V = {}, live = {}, presets = [];
const controls = new Map(); // key → update(value)
let root;

export function mount(el) {
  root = el;
  on('hello', (m) => { P = m.params; V = { ...m.values }; live = m.live; presets = m.presets; render(); });
  on('values', (m) => { Object.assign(V, m.values); for (const k of Object.keys(m.values)) update(k); if ('valves.count' in m.values) renderValves(); tiles(); });
  on('live', (m) => { live = m.live; V['engine.hours'] = m.hours; tiles(); });
  on('messages', (m) => renderMessages(m.rows));
  on('rx', (m) => renderRx(m.rows));
}

const setValue = throttle((key, value) => send({ t: 'set', key, value }), 60);

function commit(key, value) {
  V[key] = value;
  setValue(key, value);
  tiles();
}

function update(key) {
  const fn = controls.get(key);
  if (fn) fn(V[key]);
}

// ---------- controls
function control(p, opts = {}) {
  const key = p.key;
  const label = opts.label ?? p.label;
  let el;
  if (p.kind === 'bool') {
    const cb = h('input', { type: 'checkbox', checked: !!V[key], onchange: () => commit(key, cb.checked ? 1 : 0) });
    controls.set(key, (v) => { cb.checked = !!v; });
    el = h('label', { class: 'row toggle' }, h('span', { class: 'lbl' }, label), h('span', { class: 'switch' }, cb, h('i')));
  } else if (p.kind === 'select') {
    if (p.options.length <= 4 && p.options.every((o) => o[1].length <= 14)) {
      const btns = p.options.map(([v, l]) => h('button', { type: 'button', class: 'seg', 'data-v': v, onclick: () => { commit(key, v); mark(V[key]); } }, l));
      const mark = (val) => btns.forEach((b) => b.classList.toggle('on', Number(b.dataset.v) === Number(val)));
      mark(V[key]);
      controls.set(key, mark);
      el = h('div', { class: 'row' }, h('span', { class: 'lbl' }, label), h('div', { class: 'segs' }, btns));
    } else {
      const sel = h('select', { onchange: () => commit(key, Number(sel.value)) }, p.options.map(([v, l]) => h('option', { value: v }, l)));
      sel.value = V[key];
      controls.set(key, (v) => { sel.value = v; });
      el = h('label', { class: 'row' }, h('span', { class: 'lbl' }, label), sel);
    }
  } else if (p.kind === 'text') {
    const inp = h('input', { type: 'text', class: 'txt', maxlength: 2, value: V[key], onchange: () => commit(key, inp.value) });
    controls.set(key, (v) => { if (document.activeElement !== inp) inp.value = v; });
    el = h('label', { class: 'row' }, h('span', { class: 'lbl' }, label), inp);
  } else {
    const slider = (p.max - p.min) / p.step <= 20000;
    const num = h('input', { type: 'text', inputmode: 'decimal', class: 'num', value: fmt(V[key], p.step) });
    const rng = slider ? h('input', { type: 'range', min: p.min, max: p.max, step: p.step, value: V[key] }) : null;
    const apply = (v, from) => {
      if (Number.isNaN(v)) return;
      v = Math.min(p.max, Math.max(p.min, v));
      if (from !== num) num.value = fmt(v, p.step);
      if (rng && from !== rng) rng.value = v;
      commit(key, v);
    };
    if (rng) rng.addEventListener('input', () => apply(Number(rng.value), rng));
    num.addEventListener('change', () => { apply(parseNum(num.value), num); num.value = fmt(V[key], p.step); });
    num.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        e.preventDefault();
        apply((V[key] ?? 0) + (e.key === 'ArrowUp' ? 1 : -1) * p.step * (e.shiftKey ? 10 : 1));
      }
    });
    controls.set(key, (v) => {
      if (document.activeElement !== num) num.value = fmt(v, p.step);
      if (rng && document.activeElement !== rng) rng.value = v;
    });
    el = h('div', { class: 'row' + (rng ? '' : ' norange') }, h('span', { class: 'lbl' }, label), rng, h('span', { class: 'numwrap' }, num, h('span', { class: 'unit' }, p.unit)));
  }
  if (p.hint && !opts.noHint) el.title = p.hint;
  if (p.hint && !opts.noHint) return h('div', { class: 'ctl' }, el, h('div', { class: 'hint' }, p.hint));
  return el;
}

// ---------- page
function render() {
  controls.clear();
  root.replaceChildren();
  const bar = h('div', { class: 'toolbar' },
    h('span', { class: 'muted' }, 'Scenario:'),
    presets.map((name) => h('button', { class: 'btn', onclick: () => send({ t: 'preset', name }) }, name[0].toUpperCase() + name.slice(1))),
    h('span', { class: 'sep' }),
    h('button', { class: 'btn ghost', onclick: () => send({ t: 'resetDist' }) }, 'Afstand op nul'),
    h('button', { class: 'btn ghost', onclick: () => send({ t: 'resetPos' }) }, 'Positie terug naar start'));
  root.append(bar, h('div', { class: 'tiles', id: 'tiles' }));
  const grid = h('div', { class: 'grid' });
  const groups = {};
  for (const p of P) (groups[p.group] ||= []).push(p);
  for (const g of ORDER) {
    if (!groups[g]) continue;
    if (g === 'Hydrauliek') { grid.append(h('section', { class: 'card wide', id: 'valves' })); continue; }
    if (g === 'Verlichting') {
      grid.append(h('section', { class: 'card' }, h('h3', null, g), h('div', { class: 'chips' }, groups[g].map(chip))));
      continue;
    }
    const card = h('section', { class: 'card' + (g === "Werktuigcommando's" ? ' cmd' : '') }, h('h3', null, g === "Werktuigcommando's" ? 'Commando’s van werktuig volgen (TECU klasse 3)' : g));
    if (g === 'Aftakas achter' || g === 'Aftakas voor') card.append(h('div', { class: 'live', id: `live-${g === 'Aftakas achter' ? 'rearPto' : 'frontPto'}` }));
    for (const p of groups[g]) card.append(control(p));
    grid.append(card);
  }
  grid.append(h('section', { class: 'card wide', id: 'rx' }, h('h3', null, 'Ontvangen van werktuigen'), h('div', { class: 'tablewrap' })));
  grid.append(h('section', { class: 'card wide', id: 'msgs' }, h('h3', null, 'Verzonden berichten'), h('div', { class: 'tablewrap' })));
  root.append(grid);
  renderValves();
  tiles();
}

function chip(p) {
  const b = h('button', { type: 'button', class: 'chip', onclick: () => { commit(p.key, V[p.key] ? 0 : 1); mark(V[p.key]); } }, p.label);
  const mark = (v) => b.classList.toggle('on', !!v);
  mark(V[p.key]);
  controls.set(p.key, mark);
  return b;
}

function renderValves() {
  const card = $('#valves');
  if (!card) return;
  const n = V['valves.count'] ?? 0;
  for (const k of [...controls.keys()]) if (/^valve\d/.test(k)) controls.delete(k);
  const byKey = Object.fromEntries(P.map((p) => [p.key, p]));
  const rows = [];
  for (let i = 0; i < n; i++) {
    rows.push(h('tr', null, h('td', { class: 'vn' }, `${i + 1}`),
      h('td', null, control(byKey[`valve${i}.state`], { label: '', noHint: true })),
      h('td', { class: 'flow' }, control(byKey[`valve${i}.flow`], { label: '', noHint: true })),
      h('td', null, control(byKey[`valve${i}.failsafe`], { label: '', noHint: true }))));
  }
  card.replaceChildren(h('h3', null, 'Hydrauliek (aux-ventielen)'),
    control(byKey['valves.count']),
    n ? h('table', { class: 'valves' }, h('thead', null, h('tr', null, h('th', null, '#'), h('th', null, 'Stand'), h('th', null, 'Debiet'), h('th', null, 'Fail-safe'))), h('tbody', null, rows))
      : h('p', { class: 'muted' }, 'Geen ventielen: er worden geen Aux Valve-berichten verzonden.'));
}

function tiles() {
  const el = $('#tiles');
  if (!el) return;
  const dir = { 1: 'vooruit', 0: 'achteruit' }[V['drive.direction']] ?? 'n.b.';
  const dist = live.groundDist ?? 0;
  const t = [
    ['Motor', fmt(V['engine.rpm']), 'rpm', `${fmt(V['engine.torque'])} % koppel`],
    ['Snelheid', fmt(V['drive.speed'], 0.1), 'km/h', dir],
    ['Aftakas achter', fmt(live.rearPto ?? 0), 'rpm', V['rearPto.engaged'] ? `aan · ${V['rearPto.mode1000'] === 1 ? '1000' : '540'}` : 'uit'],
    ['Achterhef', fmt(V['rearHitch.position'], 0.1), '%', inWork('rearHitch') ? 'in werkstand' : 'uit werkstand'],
    ['Afstand', dist >= 1000 ? fmt(dist / 1000, 0.01) : fmt(dist), dist >= 1000 ? 'km' : 'm', `${fmt(V['engine.hours'], 0.1)} motoruren`],
    ['Positie', live.lat !== undefined ? `${live.lat.toFixed(5)}` : '—', '', live.lon !== undefined ? `${live.lon.toFixed(5)} · ${fmt(live.heading ?? 0)}°` : ''],
  ];
  if (el.children.length !== t.length) {
    el.replaceChildren(...t.map(() => h('div', { class: 'tile' }, h('div', { class: 'tl' }), h('div', { class: 'tv' }, h('b'), h('span')), h('div', { class: 'ts' }))));
  }
  t.forEach(([l, v, u, s], i) => {
    const c = el.children[i];
    c.children[0].textContent = l;
    c.children[1].children[0].textContent = v;
    c.children[1].children[1].textContent = u;
    c.children[2].textContent = s;
  });
  c('stop', V['sys.stop'] === 0);
  for (const k of ['rearPto', 'frontPto']) {
    const e = $(`#live-${k}`);
    if (e) e.textContent = `Actueel ${fmt(live[k] ?? 0)} rpm`;
  }
}

function c(name, on) { document.body.classList.toggle(name, on); }

function inWork(k) {
  const iw = V[`${k}.inWork`];
  return iw === -1 ? V[`${k}.position`] < V[`${k}.threshold`] : iw === 1;
}

// ---------- tables
let msgRows = new Map();
function renderMessages(rows) {
  const wrap = $('#msgs .tablewrap');
  if (!wrap) return;
  if (!wrap.firstChild || msgRows.size !== rows.length) {
    msgRows = new Map();
    const body = h('tbody');
    for (const r of rows) {
      const cb = h('input', { type: 'checkbox', checked: r.enabled, onchange: () => send({ t: 'msg', key: r.key, enabled: cb.checked }) });
      const iv = h('input', { type: 'text', inputmode: 'numeric', class: 'iv', value: r.interval || '', placeholder: 'aanvraag',
        onchange: () => send({ t: 'msg', key: r.key, interval: parseInt(iv.value, 10) || 0 }) });
      const cnt = h('td', { class: 'r' }), data = h('td', { class: 'mono' });
      const tr = h('tr', null, h('td', null, cb), h('td', { class: 'mono' }, hex(r.pgn, 4)), h('td', null, r.name, r.note ? h('div', { class: 'hint' }, r.note) : null),
        h('td', null, r.cf === 'gnss' ? 'GNSS' : 'TECU'), h('td', null, h('span', { class: 'ivw' }, iv, ' ms')), cnt, data,
        h('td', null, h('button', { class: 'btn small ghost', onclick: () => send({ t: 'msg', key: r.key, now: true }) }, 'Zend')));
      msgRows.set(r.key, { tr, cb, iv, cnt, data });
      body.append(tr);
    }
    wrap.replaceChildren(h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Aan', 'PGN', 'Bericht', 'Bron', 'Interval', 'Aantal', 'Laatste data', ''].map((x) => h('th', null, x)))), body));
  }
  for (const r of rows) {
    const e = msgRows.get(r.key);
    if (!e) continue;
    if (document.activeElement !== e.cb) e.cb.checked = r.enabled;
    if (document.activeElement !== e.iv) e.iv.value = r.interval || '';
    e.cnt.textContent = r.count.toLocaleString('nl-BE');
    e.data.textContent = r.data;
    e.tr.classList.toggle('dim', !r.enabled);
  }
}

function renderRx(rows) {
  const wrap = $('#rx .tablewrap');
  if (!wrap) return;
  if (!rows.length) {
    wrap.replaceChildren(h('p', { class: 'muted' }, 'Nog niets ontvangen. Werktuigen sturen hier o.a. Hitch and PTO Commands (PGN 65090), Aux Valve Commands, Machine Selected Speed Command en Maintain Power.'));
    return;
  }
  const now = Date.now() / 1000;
  rows.sort((a, b) => a.pgn - b.pgn || a.sa - b.sa);
  wrap.replaceChildren(h('table', { class: 'tbl' },
    h('thead', null, h('tr', null, ['Bron', 'PGN', 'Bericht', 'Inhoud', 'Data', 'Aantal', 'Laatst'].map((x) => h('th', null, x)))),
    h('tbody', null, rows.map((r) => h('tr', { class: now - r.t > 3 ? 'dim' : '' },
      h('td', { class: 'mono' }, '0x' + hex(r.sa)), h('td', { class: 'mono' }, hex(r.pgn, 4)), h('td', null, r.name || pgnName(r.pgn)),
      h('td', null, r.text), h('td', { class: 'mono' }, r.data), h('td', { class: 'r' }, r.count), h('td', { class: 'r' }, `${Math.max(0, now - r.t).toFixed(1)} s`))))));
}
