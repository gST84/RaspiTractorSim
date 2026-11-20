// Bus page: statistics, the control functions on the network (from Address Claimed), a live CAN log
// and a way to send a raw frame or a request.
import { h, $, hex, send, on, pgnName, splitId, isConnected } from './util.js';

let root, statsEl, nodesEl, logBody, filterIn, dirSel;
let rows = [];
let paused = false;
let visible = false;
const MAX = 1500;

export function mount(el) {
  root = el;
  statsEl = h('div', { class: 'stats' });
  nodesEl = h('div', { class: 'tablewrap' });
  filterIn = h('input', { type: 'search', placeholder: 'filter: PGN, adres of ID (bv. FE48, 0x80, E700)', oninput: renderLog });
  dirSel = h('select', { onchange: renderLog }, h('option', { value: '' }, 'rx + tx'), h('option', { value: 'rx' }, 'ontvangen'), h('option', { value: 'tx' }, 'verzonden'));
  logBody = h('tbody');
  const idIn = h('input', { type: 'text', class: 'mono', placeholder: '18EAFFF0', size: 10 });
  const dataIn = h('input', { type: 'text', class: 'mono', placeholder: '00 EE 00', size: 26 });
  const pgnIn = h('input', { type: 'text', class: 'mono', placeholder: 'FEE6', size: 7 });
  const daIn = h('input', { type: 'text', class: 'mono', placeholder: 'FF', size: 3 });
  root.append(h('div', { class: 'grid' },
    h('section', { class: 'card' }, h('h3', null, 'CAN-bus'), statsEl),
    h('section', { class: 'card wide' }, h('h3', null, 'Netwerk (Address Claimed)'), nodesEl),
    h('section', { class: 'card full' },
      h('h3', null, 'CAN-log',
        h('span', { class: 'tools' }, filterIn, dirSel,
          h('button', { class: 'btn small ghost', onclick: (e) => { paused = !paused; e.target.textContent = paused ? 'Verder' : 'Pauze'; } }, 'Pauze'),
          h('button', { class: 'btn small ghost', onclick: () => { rows = []; renderLog(); } }, 'Wissen'),
          h('a', { class: 'btn small ghost api-dl', href: '/api/frames.csv', download: '' }, 'CSV'))),
      h('div', { class: 'tablewrap log' }, h('table', { class: 'tbl mono' },
        h('thead', null, h('tr', null, ['Tijd', '', 'ID', 'PGN', 'Bericht', 'Van', 'Naar', 'Data'].map((x) => h('th', null, x)))), logBody))),
    h('section', { class: 'card' }, h('h3', null, 'Frame zenden'),
      h('div', { class: 'row' }, h('span', { class: 'lbl' }, 'ID (29 bit, hex)'), idIn),
      h('div', { class: 'row' }, h('span', { class: 'lbl' }, 'Data (hex)'), dataIn),
      h('div', { class: 'toolbar small' }, h('button', { class: 'btn', onclick: () => send({ t: 'send', id: idIn.value, data: dataIn.value }) }, 'Zenden'))),
    h('section', { class: 'card' }, h('h3', null, 'Request (PGN 59904)'),
      h('div', { class: 'row' }, h('span', { class: 'lbl' }, 'Gevraagde PGN (hex)'), pgnIn),
      h('div', { class: 'row' }, h('span', { class: 'lbl' }, 'Bestemming (hex)'), daIn),
      h('div', { class: 'toolbar small' }, h('button', { class: 'btn', onclick: () => send({ t: 'request', pgn: pgnIn.value || 'EE00', da: parseInt(daIn.value || 'FF', 16) }) }, 'Vraag aan (vanaf de TECU)')))));

  on('bus', state);
  on('frames', (m) => {
    if (m.reset) rows = [];
    if (paused && !m.reset) return;
    rows.push(...m.rows);
    if (rows.length > MAX) rows = rows.slice(-MAX);
    renderLog();
  });
}

export function show() { visible = true; if (isConnected()) send({ t: 'sub', name: 'frames', on: true }); }
export function hide() { visible = false; send({ t: 'sub', name: 'frames', on: false }); }
export function reconnect() { if (visible) send({ t: 'sub', name: 'frames', on: true }); }

function state(m) {
  const s = m.stats;
  $('#busstat').textContent = `${s.rxRate + s.txRate} fr/s · ${s.load} %`;
  $('#busstat').classList.toggle('err', !!s.errors && !!s.lastError);
  statsEl.replaceChildren(h('dl', null,
    h('dt', null, 'Interface'), h('dd', null, m.busName),
    h('dt', null, 'Ontvangen'), h('dd', null, `${s.rx.toLocaleString('nl-BE')} frames (${s.rxRate}/s)`),
    h('dt', null, 'Verzonden'), h('dd', null, `${s.tx.toLocaleString('nl-BE')} frames (${s.txRate}/s)`),
    h('dt', null, 'Busbelasting'), h('dd', null, `${s.load} % (schatting)`),
    h('dt', null, 'Foutframes'), h('dd', { class: s.errors ? 'err' : '' }, `${s.errors}${s.lastError ? ' · laatste: ' + s.lastError : ''}`),
    s.txDropped ? [h('dt', null, 'Niet verzonden'), h('dd', { class: 'err' }, String(s.txDropped))] : null));
  nodesEl.replaceChildren(h('table', { class: 'tbl' },
    h('thead', null, h('tr', null, ['Adres', 'Functie', 'NAME', 'Frames', 'Laatst', ''].map((x) => h('th', null, x)))),
    h('tbody', null, m.nodes.map((n) => h('tr', { class: n.age > 5 && !n.own ? 'dim' : '' },
      h('td', { class: 'mono' }, '0x' + hex(n.addr)), h('td', null, n.text || h('span', { class: 'muted' }, 'geen Address Claimed gezien')),
      h('td', { class: 'mono small' }, n.name || ''), h('td', { class: 'r' }, n.frames.toLocaleString('nl-BE')),
      h('td', { class: 'r' }, n.own ? '' : `${n.age} s`), h('td', null, n.own ? h('span', { class: 'pill ok' }, 'simulator') : null))))));
}

function renderLog() {
  if (!logBody) return;
  const f = filterIn.value.trim().toUpperCase().replace(/^0X/, '');
  const dir = dirSel.value;
  const out = [];
  const t0 = rows.length ? rows[rows.length - 1][0] : 0;
  for (let i = rows.length - 1; i >= 0 && out.length < 400; i--) {
    const [t, d, id, data] = rows[i];
    if (dir && d !== dir) continue;
    const { pgn, sa, da } = splitId(id);
    const idh = hex(id, 8), pg = hex(pgn, 4);
    if (f && !(idh.includes(f) || pg.includes(f) || hex(sa) === f || hex(da) === f || pgnName(pgn).toUpperCase().includes(f))) continue;
    out.push(h('tr', { class: d },
      h('td', { class: 'muted' }, (t - t0).toFixed(3)), h('td', null, d === 'rx' ? '←' : '→'), h('td', null, idh), h('td', null, pg),
      h('td', { class: 'sans' }, pgnName(pgn)), h('td', null, hex(sa)), h('td', null, da === 0xff ? 'alle' : hex(da)),
      h('td', null, data.match(/../g)?.join(' ') ?? '')));
  }
  logBody.replaceChildren(...out);
}
