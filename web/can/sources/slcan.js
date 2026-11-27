// Serial-line CAN adapters (slcan / Lawicel ASCII protocol) over WebSerial (Chrome / Edge): CANable with the
// slcan firmware, USBtin, Lawicel CANUSB and compatible adapters. The analyzer opens them listen-only ("L") when
// the adapter accepts it, else normally ("O"); the online simulator opens them normally and sends.
//   t iii l dd…    11-bit frame        T iiiiiiii l dd…   29-bit frame        r / R   remote frames (ignored)

import { frame } from '../frames.js';

export const serialAvailable = () => typeof navigator !== 'undefined' && !!navigator.serial && (typeof isSecureContext === 'undefined' || isSecureContext);

const SPEED = { 10000: 'S0', 20000: 'S1', 50000: 'S2', 100000: 'S3', 125000: 'S4', 250000: 'S5', 500000: 'S6', 800000: 'S7', 1000000: 'S8' };

const hx = (v, n) => v.toString(16).toUpperCase().padStart(n, '0');

/** A frame to send as an slcan command: T iiiiiiii l dd… or t iii l dd…, closed by a carriage return. */
export function slcanEncode(id, ext, data) {
  const len = Math.min(data.length, 8);
  let s = ext ? `T${hx(id & 0x1fffffff, 8)}${len}` : `t${hx(id & 0x7ff, 3)}${len}`;
  for (let i = 0; i < len; i++) s += hx(data[i], 2);
  return s + '\r';
}

/** One slcan line (without the carriage return) → frame or null. */
export function slcanLine(line, t) {
  const c = line[0];
  if (c !== 't' && c !== 'T') return null;
  const ext = c === 'T';
  const n = ext ? 8 : 3;
  if (line.length < 1 + n + 1) return null;
  const id = parseInt(line.substr(1, n), 16);
  const dlc = parseInt(line[1 + n], 16);
  if (!Number.isFinite(id) || !(dlc >= 0 && dlc <= 8) || line.length < 2 + n + dlc * 2) return null;
  const data = new Uint8Array(dlc);
  for (let i = 0; i < dlc; i++) data[i] = parseInt(line.substr(2 + n + i * 2, 2), 16);
  return frame(t, id, data, ext);
}

export class SlcanSource {
  /** opts.listen (default true): listen-only ("L") where the adapter can; false for the simulator, which sends.
   *  opts.baud: speed of the serial line (115200 by default; a CANable over USB ignores it). */
  constructor(port, bitrate, opts = {}) {
    this.kind = 'serial';
    this.port = port;
    this.bitrate = bitrate;
    this.listen = opts.listen !== false;
    this.baud = opts.baud || 115200;
    this.batch = 32; // frames per send() call: one write
    this.closed = false;
    this.replies = [];
    this.waiting = false;
    this.writes = Promise.resolve();
    this.txErrors = 0;
  }

  get label() { return 'Seriële adapter (slcan)'; }

  static async request(bitrate, opts) {
    return new SlcanSource(await navigator.serial.requestPort(), bitrate, opts);
  }

  /** A port this site may already use (no chooser, no click needed): the remembered one, else the first. */
  static async allowed(remember, bitrate, opts) {
    const ports = await navigator.serial.getPorts();
    const same = (p) => { const i = p.getInfo(); return remember && i.usbVendorId === remember.usbVendorId && i.usbProductId === remember.usbProductId; };
    const port = ports.find(same) || ports[0];
    return port ? new SlcanSource(port, bitrate, opts) : null;
  }

  /** What to remember to find this port again. */
  get identity() { const i = this.port.getInfo(); return { usbVendorId: i.usbVendorId, usbProductId: i.usbProductId }; }

  /** Writes text; writes never overlap (the stream has one writer at a time). */
  write(text) {
    const job = this.writes.then(async () => {
      const w = this.port.writable.getWriter();
      try { await w.write(new TextEncoder().encode(text)); } finally { w.releaseLock(); }
    });
    this.writes = job.catch(() => {});
    return job;
  }

  /** Sends frames ({id, ext, data}) in one write. */
  send(frames) { return this.write(frames.map((f) => slcanEncode(f.id, f.ext, f.data)).join('')); }

  /** Waits for the reply of a command: '\r' = OK, '\x07' = error. */
  async command(text, ms = 300) {
    this.replies.length = 0;
    this.waiting = true;
    try {
      await this.write(text);
      const t0 = Date.now();
      while (Date.now() - t0 < ms) {
        if (this.replies.length) return this.replies.shift();
        await new Promise((r) => setTimeout(r, 10));
      }
      return null;
    } finally {
      this.waiting = false;
    }
  }

  async open(sink) {
    this.sink = sink;
    const speed = SPEED[this.bitrate];
    if (!speed) throw new Error(`slcan: ${this.bitrate / 1000} kbit/s niet ondersteund`);
    await this.port.open({ baudRate: this.baud });
    this.readLoop();
    await this.command('\r\r\r');
    await this.command('C\r');
    await this.command(`${speed}\r`);
    let note = '';
    if (!this.listen) {
      if (await this.command('O\r') === 'error') throw new Error('slcan: de adapter weigert het openen (O)');
    } else if (await this.command('L\r') === 'ok') note = ' · listen-only';
    else {
      await this.command('O\r');
      note = ' · adapter kent geen listen-only (L): normaal geopend, de adapter bevestigt frames (ACK)';
    }
    sink.status(`slcan · ${this.bitrate / 1000} kbit/s${note}`);
  }

  async readLoop() {
    const dec = new TextDecoder();
    let rest = '';
    try {
      while (!this.closed && this.port.readable) {
        this.reader = this.port.readable.getReader();
        try {
          for (;;) {
            const { value, done } = await this.reader.read();
            if (done) break;
            this.sink.activity?.();
            rest += dec.decode(value, { stream: true });
            const out = [];
            const t = Date.now() / 1000;
            let i;
            while ((i = rest.search(/[\r\x07]/)) >= 0) {
              const line = rest.slice(0, i);
              const end = rest[i];
              rest = rest.slice(i + 1);
              const f = slcanLine(line, t);
              if (f) out.push(f);
              else if (end === '\x07') { if (this.waiting) this.replies.push('error'); else this.txErrors++; } // bell: refused (TX buffer full)
              else if (this.waiting && (line === '' || line === 'z' || line === 'Z')) this.replies.push('ok');
            }
            if (rest.length > 4096) rest = '';
            if (out.length) this.sink.frames(out);
          }
        } finally {
          this.reader.releaseLock();
        }
      }
    } catch (e) {
      if (!this.closed) { this.closed = true; this.sink.closed(`Serieel: ${e.message || e}`); }
    }
  }

  async close() {
    this.closed = true;
    try { await this.write('C\r'); } catch { /* port gone */ }
    try { await this.reader?.cancel(); } catch { /* ignore */ }
    try { await this.port.close(); } catch { /* ignore */ }
  }
}
