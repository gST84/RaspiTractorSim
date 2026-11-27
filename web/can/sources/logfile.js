// CAN logs as a frame source: open a file at once or play it back in real time.
//
// Formats (recognised from the content):
//   microchip  Microchip CAN BUS Analyzer (.CAN): "tijd;richting;ID[x];DLC;B0;B1;…", decimal comma
//   candump    candump -l: "(1690000000.123456) can0 18FEF100#0102030405060708", and the plain candump output
//   trc        PCAN-View trace (.trc) version 1.x and 2.x (time in ms)
//   csv        the CSV export of the Bus page: "tijd;richting;id;pgn;bron;doel;data"

import { frame, hex } from '../frames.js';

const HEX = /^[0-9a-f]+$/i;

function bytesFrom(list) {
  const out = [];
  for (const x of list) { if (!HEX.test(x) || x.length > 2) break; out.push(parseInt(x, 16)); }
  return out;
}

function microchip(line) {
  const p = line.trim().split(';');
  if (p.length < 4) return null;
  const t = Number(p[0].replace(',', '.'));
  if (!Number.isFinite(t)) return null;
  const idText = p[2].trim();
  const ext = /x$/i.test(idText);
  const id = parseInt(idText.replace(/x$/i, ''), 16);
  const dlc = parseInt(p[3], 10);
  if (!Number.isFinite(id) || !(dlc >= 0 && dlc <= 8)) return null;
  const data = bytesFrom(p.slice(4, 4 + dlc).map((x) => x.trim()));
  return frame(t, id, data.slice(0, dlc), ext || id > 0x7ff, /tx/i.test(p[1]) ? 'tx' : 'rx');
}

const CANDUMP_L = /^\s*\((\d+(?:\.\d+)?)\)\s+\S+\s+([0-9a-f]{1,8})#(R|[0-9a-f]*)/i;
const CANDUMP = /^\s*(?:\((\d+(?:\.\d+)?)\)\s+)?\S+\s+([0-9a-f]{3,8})\s+\[(\d)\]\s*((?:[0-9a-f]{2}\s*)*)/i;

function candump(line, n) {
  let m = CANDUMP_L.exec(line);
  if (m) {
    const d = m[3] === 'R' || m[3] === 'r' ? [] : (m[3].match(/../g) || []).map((x) => parseInt(x, 16));
    return frame(Number(m[1]), parseInt(m[2], 16), d.slice(0, 8), m[2].length > 3);
  }
  m = CANDUMP.exec(line);
  if (m) {
    const d = (m[4].trim().match(/[0-9a-f]{2}/gi) || []).map((x) => parseInt(x, 16)).slice(0, Number(m[3]));
    return frame(m[1] !== undefined ? Number(m[1]) : n * 0.001, parseInt(m[2], 16), d, m[2].length > 3);
  }
  return null;
}

function trc(line) {
  const s = line.trim();
  if (!s || s.startsWith(';')) return null;
  const tok = s.split(/\s+/);
  if (tok.length < 4) return null;
  const t = Number(tok[1]) / 1000;
  if (!Number.isFinite(t)) return null;
  let di = tok.findIndex((x, i) => i > 1 && (x === 'Rx' || x === 'Tx'));
  let idTok, rest;
  if (di < 0) { idTok = tok[2]; rest = tok.slice(3); } // version 1.0: no direction
  else if (HEX.test(tok[di - 1]) && tok[di - 1].length >= 3 && di - 1 > 1) { idTok = tok[di - 1]; rest = tok.slice(di + 1); } // 2.x
  else { idTok = tok[di + 1]; rest = tok.slice(di + 2); } // 1.1
  if (!idTok || !HEX.test(idTok)) return null;
  while (rest.length && rest[0] === '-') rest.shift();
  const dlc = parseInt(rest[0], 10);
  if (!(dlc >= 0 && dlc <= 8)) return null;
  const data = bytesFrom(rest.slice(1, 1 + dlc));
  const id = parseInt(idTok, 16);
  return frame(t, id, data, idTok.length > 4 || id > 0x7ff, di >= 0 && tok[di] === 'Tx' ? 'tx' : 'rx');
}

function csv(line) {
  const p = line.trim().split(';');
  if (p.length < 7) return null;
  const t = Number(p[0].replace(',', '.'));
  if (!Number.isFinite(t)) return null;
  const id = parseInt(p[2], 16);
  const d = (p[6].replace(/\s/g, '').match(/../g) || []).map((x) => parseInt(x, 16));
  return frame(t, id, d.slice(0, 8), p[2].trim().length > 3, p[1] === 'tx' ? 'tx' : 'rx');
}

export function detect(text) {
  const lines = text.split(/\r?\n/, 60).filter((l) => l.trim());
  if (lines.some((l) => /^tijd;richting;id;pgn/i.test(l))) return 'csv';
  if (lines.some((l) => /^;\$FILEVERSION|^;\s*Message Number|^\s*\d+\)\s+\d/.test(l))) return 'trc';
  if (lines.some((l) => CANDUMP_L.test(l) || CANDUMP.test(l))) return 'candump';
  if (lines.some((l) => microchip(l))) return 'microchip';
  if (lines.some((l) => trc(l))) return 'trc';
  return null;
}

const PARSERS = { microchip, candump, trc, csv };
export const FORMAT_NAMES = { microchip: 'Microchip CAN BUS Analyzer', candump: 'candump', trc: 'PCAN-View (.trc)', csv: 'CSV van de Bus-pagina' };

/** Text of a log → { format, frames, skipped } (frames sorted by time). */
export function parseLog(text) {
  const format = detect(text);
  if (!format) throw new Error('Onbekend logformaat (verwacht: Microchip .CAN, candump, PCAN .trc of de CSV van de Bus-pagina)');
  const parse = PARSERS[format];
  const frames = [];
  let skipped = 0, n = 0;
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const f = parse(line, n++);
    if (f) frames.push(f); else skipped++;
  }
  let sorted = true;
  for (let i = 1; i < frames.length; i++) if (frames[i].t < frames[i - 1].t) { sorted = false; break; }
  if (!sorted) frames.sort((a, b) => a.t - b.t);
  return { format, frames, skipped };
}

/** One frame as a candump -l line (for recording). */
export function toCandump(f, iface = 'can0') {
  const id = f.ext ? hex(f.id, 8) : hex(f.id, 3);
  return `(${f.t.toFixed(6)}) ${iface} ${id}#${Array.from(f.data, (b) => hex(b)).join('')}`;
}

/**
 * A log file as a source.  mode 'load': everything at once (in chunks, so the page stays responsive);
 * mode 'play': in real time (speed × log time), with pause.
 */
export class LogSource {
  constructor(name, text, mode = 'load', speed = 1) {
    this.kind = 'log';
    this.name = name;
    this.text = text;
    this.mode = mode;
    this.speed = speed;
    this.timer = null;
    this.paused = false;
    this.pos = 0;
  }

  get label() { return `Logbestand ${this.name}`; }

  async open(sink) {
    this.sink = sink;
    const r = parseLog(this.text);
    this.text = null;
    this.frames = r.frames;
    this.info = `${FORMAT_NAMES[r.format]}, ${r.frames.length.toLocaleString('nl-BE')} frames${r.skipped ? `, ${r.skipped} regels overgeslagen` : ''}`;
    sink.status(this.info);
    if (!this.frames.length) { sink.closed('Het bestand bevat geen frames.'); return; }
    if (this.mode === 'load') this.loadChunk();
    else { this.t0 = performance.now(); this.l0 = this.frames[0].t; this.tick(); }
  }

  loadChunk() {
    const end = Math.min(this.frames.length, this.pos + 25000);
    this.sink.frames(this.frames.slice(this.pos, end));
    this.pos = end;
    this.sink.progress?.(this.pos / this.frames.length);
    if (this.pos < this.frames.length) this.timer = setTimeout(() => this.loadChunk(), 0);
    else this.sink.closed(null, `${this.info}, ingelezen`);
  }

  tick() {
    this.timer = null;
    if (this.paused) return;
    const logNow = this.l0 + ((performance.now() - this.t0) / 1000) * this.speed;
    const start = this.pos;
    while (this.pos < this.frames.length && this.frames[this.pos].t <= logNow) this.pos++;
    if (this.pos > start) this.sink.frames(this.frames.slice(start, this.pos));
    this.sink.progress?.(this.pos / this.frames.length);
    if (this.pos >= this.frames.length) { this.sink.closed(null, `${this.info}, afgespeeld`); return; }
    this.timer = setTimeout(() => this.tick(), 50);
  }

  setPaused(p) {
    if (this.mode !== 'play' || p === this.paused) return;
    this.paused = p;
    if (!p) { // continue from the current log position
      this.l0 = this.frames[Math.min(this.pos, this.frames.length - 1)].t;
      this.t0 = performance.now();
      this.tick();
    }
  }

  close() { clearTimeout(this.timer); this.timer = null; this.frames = []; }
}
