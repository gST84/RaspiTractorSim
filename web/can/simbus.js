// The online version (Pyodide) on a real CAN bus. Instead of the simulated bus with the demo implement, the
// simulator sends and receives through a CAN adapter of this computer: USB (WebUSB: Microchip CAN BUS Analyzer,
// candleLight, PCAN-USB) or serial (WebSerial: slcan). The choice is stored per browser ('tractorsim.bus');
// switching between demo and adapter restarts the page, the adapter itself connects and disconnects live.
//   page → simulator   py.receive(JSON [[id, ext, hex], …]), py.state(connected, label)
//   simulator → page   send(id, ext, hex) → 1 queued, 0 no adapter (frame dropped), 2 queue full (try again)

import { UsbSource, usbAvailable } from './sources/usb.js';
import { SlcanSource, serialAvailable } from './sources/slcan.js';

const KEY = 'tractorsim.bus';
export const BUS_DEFAULTS = { demo: true, kind: 'usb', bitrate: 250000, baud: 115200, device: null };
export const BITRATES = [125000, 250000, 500000, 1000000];
export const BAUDS = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600, 1000000, 2000000, 3000000];
const TX_MAX = 512;

const storage = () => { try { return globalThis.localStorage; } catch { return null; } };

export function loadBusConfig(store = storage()) {
  try { return { ...BUS_DEFAULTS, ...JSON.parse(store?.getItem(KEY) || '{}') }; } catch { return { ...BUS_DEFAULTS }; }
}

export function saveBusConfig(cfg, store = storage()) {
  try { store?.setItem(KEY, JSON.stringify(cfg)); } catch { /* private window: only for this visit */ }
}

const hexOf = (d) => { let s = ''; for (const b of d) s += (b < 16 ? '0' : '') + b.toString(16); return s; };
const bytesOf = (h) => { const b = new Uint8Array(h.length >> 1); for (let i = 0; i < b.length; i++) b[i] = parseInt(h.substr(i * 2, 2), 16); return b; };

export class SimBus {
  /** Safety net for connecting; the drivers also time out each step themselves. */
  static OPEN_MS = 15000;

  constructor(store) {
    this.store = store;
    this.cfg = loadBusConfig(store);
    this.py = null;
    this.src = null;
    this.txq = [];
    this.pumping = false;
    this.manualOff = false;
    this.st = { connected: false, connecting: false, label: '', text: '', error: '', rx: 0, tx: 0, dropped: 0 };
    this.fns = new Set();
  }

  /** True when the simulator of this page runs on the adapter (not on the demo bus). */
  get active() { return !!this.py; }

  onChange(fn) { this.fns.add(fn); return () => this.fns.delete(fn); }

  emit() {
    for (const fn of this.fns) {
      try { fn(this.st); } catch (e) { console.error(e); }
    }
  }

  save(patch) {
    Object.assign(this.cfg, patch);
    saveBusConfig(this.cfg, this.store);
    this.emit();
  }

  /** The simulator: { receive(text), state(connected, label) }. */
  attach(py) {
    this.py = py;
    this.watchPlug();
  }

  /** A frame from the simulator. */
  send(id, ext, hex) {
    if (!this.src || !this.st.connected) return 0;
    if (this.txq.length >= TX_MAX) return 2;
    this.txq.push({ id, ext: !!ext, data: bytesOf(hex) });
    this.pump();
    return 1;
  }

  async pump() {
    if (this.pumping) return;
    this.pumping = true;
    try {
      while (this.txq.length && this.src) {
        const src = this.src;
        const batch = this.txq.splice(0, src.batch || 1);
        try {
          await src.send(batch);
          this.st.tx += batch.length;
        } catch (e) {
          this.st.dropped += batch.length;
          this.st.error = `zenden: ${e.message || e}`;
          this.emit();
          if (src.closed) break;
        }
      }
    } finally {
      this.pumping = false;
    }
  }

  /** Connects with the stored settings. `ask`: let the user pick the adapter (needs a click); else an adapter
   *  this site may already use. Returns true when connected. */
  async connect(ask = false) {
    if (this.st.connecting) return false;
    await this.disconnect(true);
    this.manualOff = false;
    Object.assign(this.st, { connecting: true, error: '', text: 'verbinden…' });
    this.emit();
    try {
      const opts = { listen: false, baud: this.cfg.baud };
      let src;
      if (this.cfg.kind === 'serial') {
        if (!serialAvailable()) throw new Error('deze browser kent geen WebSerial: gebruik Chrome of Edge');
        src = ask ? await SlcanSource.request(this.cfg.bitrate, opts) : await SlcanSource.allowed(this.cfg.device, this.cfg.bitrate, opts);
      } else {
        if (!usbAvailable()) throw new Error('deze browser kent geen WebUSB: gebruik Chrome of Edge');
        src = ask ? await UsbSource.request(this.cfg.bitrate, opts) : await UsbSource.allowed(this.cfg.device, this.cfg.bitrate, opts);
      }
      if (!src) {
        this.st.text = 'geen adapter: klik op Verbinden en kies je adapter';
        return false;
      }
      await this.start(src);
      this.save({ device: src.identity });
      return true;
    } catch (e) {
      this.st.text = 'niet verbonden';
      // NotFoundError: chooser closed; manualOff: stopped with Verbreken
      if (e?.name !== 'NotFoundError' && !this.manualOff) this.st.error = e?.message || String(e);
      return false;
    } finally {
      this.st.connecting = false;
      this.emit();
    }
  }

  /** Opens a source and couples it to the simulator (also used by the tests with a made-up source). */
  async start(src) {
    const sink = {
      frames: (fs) => {
        this.st.rx += fs.length;
        this.py?.receive(JSON.stringify(fs.map((f) => [f.id, f.ext ? 1 : 0, hexOf(f.data)])));
      },
      status: (text) => { this.st.text = text; this.emit(); },
      lost: () => {},
      closed: (why) => { if (this.src === src) this.lost(why); },
    };
    this.src = src;
    let timer;
    try {
      await Promise.race([src.open(sink), new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('de adapter antwoordt niet (time-out bij het verbinden)')), SimBus.OPEN_MS);
      })]);
    } catch (e) {
      if (this.src === src) this.src = null;
      try { await src.close(); } catch { /* not open */ }
      throw e;
    } finally {
      clearTimeout(timer);
    }
    if (this.src !== src) { // Verbreken while connecting
      try { await src.close(); } catch { /* already closed */ }
      throw new Error('verbinden afgebroken');
    }
    Object.assign(this.st, { connected: true, label: src.label, error: '' });
    this.py?.state(true, src.label);
    this.emit();
  }

  /** The adapter went away (unplugged, USB error). */
  lost(why) {
    this.src = null;
    this.txq.length = 0;
    Object.assign(this.st, { connected: false, text: 'niet verbonden', error: why });
    this.py?.state(false, '');
    this.emit();
  }

  /** Closes the adapter, also one that is still connecting. `quiet`: part of a reconnect, no status change shown. */
  async disconnect(quiet = false) {
    const src = this.src;
    this.src = null;
    this.txq.length = 0;
    if (src) {
      try { await src.close(); } catch { /* already gone */ }
    }
    if (this.st.connected) {
      this.st.connected = false;
      this.py?.state(false, '');
    }
    if (!quiet) {
      this.manualOff = true;
      this.st.text = 'niet verbonden';
      this.emit();
    }
  }

  /** An adapter this site may use that is plugged in again connects by itself (unless disconnected by hand). */
  watchPlug() {
    if (this.watching || typeof navigator === 'undefined') return;
    this.watching = true;
    const again = () => {
      if (!this.src && !this.st.connecting && !this.manualOff) setTimeout(() => this.connect(false), 500);
    };
    navigator.usb?.addEventListener('connect', again);
    navigator.serial?.addEventListener('connect', again);
  }
}

/** The one instance of the page. */
export const simBus = new SimBus();
