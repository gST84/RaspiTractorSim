// Settings page: the simulated control functions (tractor ECU, virtual terminal, GNSS, task controller) and, in the online
// version, the CAN bus: the demo bus or a CAN adapter of this computer (web/can/simbus.js).
import { h, hex, send, on, isDemo } from './util.js';
import { simBus, BITRATES, BAUDS } from './can/simbus.js';
import { usbAvailable } from './can/sources/usb.js';
import { serialAvailable } from './can/sources/slcan.js';

let root, busCard = null;
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
const TC_FIELDS = [
  ['version', 'TC-versie', 'select', 'in het versiebericht aan de werktuigen', [[3, '3'], [4, '4']]],
  ['booms', 'Booms (sectiebesturing)', 'int', 'versiebericht: aantal booms voor TC-SC'],
  ['sections', 'Secties (sectiebesturing)', 'int', 'versiebericht: aantal secties voor TC-SC'],
  ['channels', 'Kanalen (taakkaart)', 'int', 'versiebericht: aantal kanalen voor TC-GEO'],
];
const TITLES = { tecu: 'Tractor-ECU', vt: 'Virtuele terminal', gnss: 'GNSS-ontvanger', tc: 'Task controller' };

export function mount(el) {
  root = el;
  on('hello', (m) => render(m.cfCfg, m.busSpec));
}

function render(cfg, busSpec) {
  root.replaceChildren(
    h('div', { class: 'note' }, h('b', null, 'Op een echte tractorbus: '),
      'zet de Tractor-ECU (en de GNSS-ontvanger) uit, anders spreken twee TECU’s elkaar tegen. ',
      'De VT kan wel blijven: kies dan een vrij adres en functie-instantie 1 als er al een terminal is. Hetzelfde voor de task controller (bv. de display van de tractor).'),
    h('div', { class: 'grid' },
      isDemo() ? (busCard ||= canCard()) : null,
      ['tecu', 'vt', 'gnss', 'tc'].filter((key) => cfg[key]).map((key) => section(key, cfg[key])),
      isDemo() ? null : h('section', { class: 'card' }, h('h3', null, 'Systeem'),
        h('dl', null, h('dt', null, 'CAN-interface'), h('dd', { class: 'mono' }, busSpec),
          h('dt', null, 'Bitrate'), h('dd', null, 'ISOBUS: 250 kbit/s, in te stellen met ', h('code', null, 'ip link'), ' (zie README)')),
        h('div', { class: 'toolbar small' }, h('a', { class: 'btn ghost api-dl', href: '/api/config', download: '' }, 'Instellingen downloaden')))));
}

const DEMO_TEXT = 'Alles draait in deze browser: tractor-ECU, VT, GNSS en een testwerktuig op een gesimuleerde CAN-bus.';
const BUS_TEXT = 'De simulator draait in deze browser en zendt en ontvangt via je CAN-adapter: tractor-ECU, VT en GNSS op '
  + 'een echte bus, zonder testwerktuig.';
const KINDS = [
  ['usb', 'USB: Microchip CAN BUS Analyzer, candleLight, PCAN-USB', usbAvailable],
  ['serial', 'Serieel (slcan): CANable, USBtin, Lawicel', serialAvailable],
];

/** Online version: demo bus or the CAN adapter (switching restarts the page; the adapter connects live). */
function canCard() {
  const card = h('section', { class: 'card' });
  const onBus = simBus.active; // what this page runs now
  let wantDemo = !onBus;
  let statusEl, connectBtn, offBtn;

  const select = (label, value, options, onchange, hint) => {
    const sel = h('select', { onchange: () => onchange(sel.value) },
      options.map(([v, text, ok]) => h('option', { value: v, disabled: ok && !ok() }, text)));
    sel.value = String(value);
    return h('label', { class: 'row', title: hint || '' }, h('span', { class: 'lbl' }, label), sel);
  };
  const change = (patch) => {
    simBus.save(patch);
    draw();
    if (onBus && (simBus.st.connected || patch.kind)) simBus.connect(false); // same adapter, new settings
  };

  function status() {
    if (!statusEl) return;
    const st = simBus.st;
    const hint = st.text && st.text !== 'niet verbonden' ? st.text : 'Klik op Verbinden en kies je adapter.';
    statusEl.replaceChildren(...[
      h('span', { class: 'pill ' + (st.connected ? 'ok' : st.connecting ? 'warn' : 'err') }, st.connected ? 'verbonden' : st.connecting ? 'verbinden…' : 'niet verbonden'),
      st.connected || st.connecting ? ` ${st.text}` : null,
      st.connected && st.txqMax >= 100 ? h('div', { class: 'err small' }, `zendwachtrij liep op tot ${st.txqMax} frames: de adapter zendt trager dan de simulator, antwoorden komen te laat`) : null,
      st.error ? h('div', { class: 'err small' }, st.error) : null,
      !st.connected && !st.connecting && !st.error ? h('div', { class: 'muted small' }, hint) : null,
      st.error && st.log.length ? h('details', { class: 'small', open: true }, h('summary', null, 'Verloop'),
        h('pre', { class: 'mono muted', style: { whiteSpace: 'pre-wrap', margin: '4px 0 0' } }, st.log.join('\n'))) : null,
    ].filter(Boolean));
    if (connectBtn) connectBtn.disabled = st.connecting;
    if (offBtn) offBtn.disabled = !st.connected && !st.connecting; // also stops a connection attempt
  }

  function draw() {
    const c = simBus.cfg;
    const demoBox = h('input', { type: 'checkbox', checked: wantDemo, onchange: (e) => { wantDemo = e.target.checked; draw(); } });
    const rows = [
      h('label', { class: 'row toggle', title: 'Aan: gesimuleerde bus met testwerktuig. Uit: een echte CAN-bus via je adapter.' },
        h('span', { class: 'lbl' }, 'Demo'), h('span', { class: 'switch' }, demoBox, h('i'))),
      h('p', { class: 'muted small' }, wantDemo ? DEMO_TEXT : BUS_TEXT),
    ];
    if (!wantDemo) {
      if (!usbAvailable() && !serialAvailable()) {
        rows.push(h('p', { class: 'err small' }, 'Deze browser kent geen WebUSB of WebSerial: gebruik Chrome of Edge (op https of localhost).'));
      }
      rows.push(
        select('Adapter', c.kind, KINDS, (v) => change({ kind: v, device: null })),
        select('Bitrate', c.bitrate, BITRATES.map((b) => [b, `${b / 1000} kbit/s`]), (v) => change({ bitrate: Number(v) }), 'ISOBUS: 250 kbit/s'),
        c.kind === 'serial' ? select('Baudrate (serieel)', c.baud, BAUDS.map((b) => [b, String(b)]), (v) => change({ baud: Number(v) }),
          'snelheid van de seriële lijn; een CANable via USB negeert ze') : null);
    }
    statusEl = connectBtn = offBtn = null;
    let tools;
    if (wantDemo === onBus) { // switch changed: restart in the other mode
      tools = [h('button', { class: 'btn primary', onclick: () => { simBus.save({ demo: wantDemo }); location.reload(); } }, 'Toepassen'),
        h('span', { class: 'muted small' }, 'herstart de pagina')];
    } else if (onBus) {
      connectBtn = h('button', { class: 'btn primary', onclick: () => simBus.connect(true) }, 'Verbinden…');
      offBtn = h('button', { class: 'btn ghost', onclick: () => simBus.disconnect() }, 'Verbreken');
      tools = [connectBtn, offBtn];
      statusEl = h('div', { class: 'small', role: 'status' });
    }
    card.replaceChildren(...[h('h3', null, 'CAN-bus'), ...rows,
      statusEl ? h('div', { class: 'row', style: { alignItems: 'flex-start', paddingTop: '6px' } }, h('span', { class: 'lbl' }, 'Status'), statusEl) : null,
      tools ? h('div', { class: 'toolbar small' }, tools) : null,
      !wantDemo ? h('p', { class: 'muted small' }, 'Houd dit tabblad open: de simulator draait in deze pagina. Is het venster verborgen of '
        + 'bedekt, dan vertraagt de browser zijn timers; de simulator houdt dan zijn ritme op het CAN-verkeer (een PLC op de bus volstaat). '
        + 'De browser onthoudt je adapter; zit hij erin, dan verbindt hij bij het openen vanzelf.') : null,
    ].filter(Boolean));
    status();
  }

  simBus.onChange(status);
  draw();
  return card;
}

function section(key, c) {
  const vals = {};
  const rows = [...FIELDS, ...(key === 'vt' ? VT_FIELDS : key === 'tc' ? TC_FIELDS : [])].map(([k, label, kind, hint, opts]) => {
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
