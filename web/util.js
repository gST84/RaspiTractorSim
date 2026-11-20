// DOM helpers, formatting and the WebSocket connection to the simulator.

/** h('div', {class: 'x', onclick}, ...children). Attributes starting with "on" become listeners. */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k === 'value') el.value = v;
      else if (k === 'checked') el.checked = !!v;
      else if (k === 'html') el.innerHTML = v;
      else el.setAttribute(k, v === true ? '' : v);
    }
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const hex = (v, n = 2) => (v === null || v === undefined ? '—' : Number(v).toString(16).toUpperCase().padStart(n, '0'));

export function fmt(v, step = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const dec = step >= 1 ? 0 : Math.min(6, Math.ceil(-Math.log10(step) - 1e-9));
  return Number(v).toLocaleString('nl-BE', { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

/** Parses a number typed with a decimal comma or point. */
export const parseNum = (s) => Number(String(s).trim().replace(/\s/g, '').replace(',', '.'));

export function toast(text, error = false) {
  const box = $('#toasts');
  const el = h('div', { class: 'toast' + (error ? ' err' : '') }, text);
  box.append(el);
  setTimeout(() => el.classList.add('out'), 4000);
  setTimeout(() => el.remove(), 4600);
}

export function throttle(fn, ms) {
  let last = 0, timer = null, args = null;
  return (...a) => {
    args = a;
    const now = Date.now();
    if (now - last >= ms) { last = now; fn(...args); return; }
    clearTimeout(timer);
    timer = setTimeout(() => { last = Date.now(); fn(...args); }, ms - (now - last));
  };
}

// ---------- connection
const handlers = {};
let ws = null;
let connected = false;
const connListeners = [];

export const on = (type, fn) => { (handlers[type] ||= []).push(fn); };
export const onConnection = (fn) => connListeners.push(fn);
export const isConnected = () => connected;

// In the browser demo the simulator runs in this page (Pyodide) and sets globalThis.tractorsimBridge:
// { attach(onText), send(text) } with the same JSON texts as the WebSocket.
const bridge = () => globalThis.tractorsimBridge;

/** True in the browser demo (no server: no downloads from /api). */
export const isDemo = () => !!bridge();

function dispatch(text) {
  let m;
  try { m = JSON.parse(text); } catch { return; }
  for (const fn of handlers[m.t] || []) {
    try { fn(m); } catch (err) { console.error(m.t, err); }
  }
}

export function send(obj) {
  if (bridge()) bridge().send(JSON.stringify(obj));
  else if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
}

export function connect() {
  if (bridge()) {
    connected = true;
    connListeners.forEach((f) => f(true));
    bridge().attach(dispatch);
    return;
  }
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onopen = () => { connected = true; connListeners.forEach((f) => f(true)); };
  ws.onclose = () => {
    if (connected) connListeners.forEach((f) => f(false));
    connected = false;
    setTimeout(connect, 1500);
  };
  ws.onmessage = (e) => dispatch(e.data);
}

/** Name of a PGN for logs and tables. */
const PGN_NAMES = {
  0xE600: 'VT → ECU', 0xE700: 'ECU → VT', 0xE800: 'Acknowledgement', 0xEA00: 'Request', 0xEB00: 'TP.DT', 0xEC00: 'TP.CM',
  0xEE00: 'Address Claimed', 0xC700: 'ETP.DT', 0xC800: 'ETP.CM', 0xCB00: 'Process Data (TC)', 0xEF00: 'Proprietary A',
  0xF004: 'EEC1', 0xF005: 'ETC2', 0xF022: 'Machine Selected Speed', 0xFD02: 'All Implements Stop', 0xFD43: 'Machine Selected Speed Cmd',
  0xFE0C: 'Working Set Member', 0xFE0D: 'Working Set Master', 0xFE0F: 'Language Command', 0xFE40: 'Lighting Data', 0xFE41: 'Lighting Command',
  0xFE42: 'Hitch & PTO Commands', 0xFE43: 'Rear PTO', 0xFE44: 'Front PTO', 0xFE45: 'Rear Hitch', 0xFE46: 'Front Hitch',
  0xFE47: 'Maintain Power', 0xFE48: 'Wheel-based Speed', 0xFE49: 'Ground-based Speed', 0xFEDA: 'Software ID', 0xFECA: 'DM1',
  0xFEE5: 'Engine Hours', 0xFEE6: 'Time/Date', 0xFEE8: 'Vehicle Dir/Speed', 0xFEEE: 'ET1', 0xFEEF: 'EFL/P1', 0xFEF1: 'CCVS1',
  0xFEF2: 'LFE1', 0xFEF3: 'Vehicle Position', 0xFEF5: 'AMB', 0xFEF7: 'VEP1', 0xFEFC: 'DD1',
  0x1F801: 'N2K Position Rapid', 0x1F802: 'N2K COG/SOG Rapid', 0x1F805: 'N2K GNSS Position', 0x1F119: 'N2K Attitude',
  0xAC00: 'Guidance Machine Status', 0xAD00: 'Guidance System Cmd',
};
export function pgnName(p) {
  if (PGN_NAMES[p]) return PGN_NAMES[p];
  if (p >= 0xFE10 && p <= 0xFE1F) return `Aux Valve ${p - 0xFE10} Est. Flow`;
  if (p >= 0xFE20 && p <= 0xFE2F) return `Aux Valve ${p - 0xFE20} Meas. Flow`;
  if (p >= 0xFE30 && p <= 0xFE3F) return `Aux Valve ${p - 0xFE30} Command`;
  if (p >= 0xFF00 && p <= 0xFFFF) return 'Proprietary B';
  return '';
}

export function splitId(id) {
  const prio = (id >>> 26) & 7;
  let pgn = (id >>> 8) & 0x3ffff;
  const pf = (pgn >> 8) & 0xff;
  const sa = id & 0xff;
  let da = 0xff;
  if (pf < 240) { da = pgn & 0xff; pgn &= 0x3ff00; }
  return { prio, pgn, sa, da };
}
