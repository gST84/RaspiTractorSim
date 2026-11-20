// Settings page: the three simulated control functions (tractor ECU, virtual terminal, GNSS).
import { h, hex, send, on } from './util.js';

let root;
const FIELDS = [
  ['enabled', 'Ingeschakeld', 'bool'],
  ['address', 'Voorkeursadres', 'hex', 'bv. F0 voor de TECU, 26 voor de eerste VT'],
  ['selfConfigurable', 'Zelfconfigureerbaar adres', 'bool', 'kiest een ander adres (128–247) als het eigen adres bezet is'],
  ['identity', 'Identiteitsnummer (NAME)', 'int'],
  ['manufacturer', 'Fabrikantcode (NAME)', 'int'],
  ['functionInstance', 'Functie-instantie (NAME)', 'int', 'VT: 0 = primaire terminal'],
];
const VT_FIELDS = [
  ['version', 'VT-versie', 'select', 'antwoord op Get Memory', [[3, '3'], [4, '4'], [5, '5'], [6, '6']]],
  ['dmSize', 'Datamasker (pixels)', 'int', 'breedte = hoogte, antwoord op Get Hardware'],
  ['skWidth', 'Softkey breedte (pixels)', 'int'],
  ['skHeight', 'Softkey hoogte (pixels)', 'int'],
  ['skCount', 'Fysieke softkeys', 'int'],
  ['virtualKeys', 'Virtuele softkeys per masker', 'int'],
  ['strict', 'Strenge poolcontrole', 'bool', 'pool weigeren bij verwijzingen naar onbestaande objecten'],
];
const TITLES = { tecu: 'Tractor-ECU', vt: 'Virtuele terminal', gnss: 'GNSS-ontvanger' };

export function mount(el) {
  root = el;
  on('hello', (m) => render(m.cfCfg, m.busSpec));
}

function render(cfg, busSpec) {
  root.replaceChildren(
    h('div', { class: 'note' }, h('b', null, 'Op een echte tractorbus: '),
      'zet de Tractor-ECU (en de GNSS-ontvanger) uit, anders spreken twee TECU’s elkaar tegen. ',
      'De VT kan wel blijven: kies dan een vrij adres en functie-instantie 1 als er al een terminal is.'),
    h('div', { class: 'grid' },
      ['tecu', 'vt', 'gnss'].map((key) => section(key, cfg[key])),
      h('section', { class: 'card' }, h('h3', null, 'Systeem'),
        h('dl', null, h('dt', null, 'CAN-interface'), h('dd', { class: 'mono' }, busSpec),
          h('dt', null, 'Bitrate'), h('dd', null, 'ISOBUS: 250 kbit/s, in te stellen met ', h('code', null, 'ip link'), ' (zie README)')),
        h('div', { class: 'toolbar small' }, h('a', { class: 'btn ghost api-dl', href: '/api/config', download: '' }, 'Instellingen downloaden')))));
}

function section(key, c) {
  const vals = {};
  const rows = [...FIELDS, ...(key === 'vt' ? VT_FIELDS : [])].map(([k, label, kind, hint, opts]) => {
    let inp;
    if (kind === 'bool') {
      inp = h('input', { type: 'checkbox', checked: !!c[k] });
      vals[k] = () => inp.checked;
      return h('label', { class: 'row toggle', title: hint || '' }, h('span', { class: 'lbl' }, label), h('span', { class: 'switch' }, inp, h('i')));
    }
    if (kind === 'select') {
      inp = h('select', null, opts.map(([v, l]) => h('option', { value: v }, l)));
      inp.value = c[k];
      vals[k] = () => Number(inp.value);
    } else if (kind === 'hex') {
      inp = h('input', { type: 'text', class: 'mono', size: 4, value: hex(c[k]) });
      vals[k] = () => parseInt(inp.value, 16);
    } else {
      inp = h('input', { type: 'text', inputmode: 'numeric', size: 8, value: c[k] });
      vals[k] = () => parseInt(inp.value, 10);
    }
    return h('label', { class: 'row', title: hint || '' }, h('span', { class: 'lbl' }, label), inp);
  });
  const apply = () => {
    const values = {};
    for (const [k, fn] of Object.entries(vals)) {
      const v = fn();
      if (typeof v === 'number' && Number.isNaN(v)) continue;
      values[k] = v;
    }
    send({ t: 'cf', key, values });
  };
  return h('section', { class: 'card' }, h('h3', null, TITLES[key]), rows,
    h('div', { class: 'toolbar small' }, h('button', { class: 'btn primary', onclick: apply }, 'Toepassen'),
      h('span', { class: 'muted small' }, 'herstart de ECU’s (nieuwe address claim)')));
}
