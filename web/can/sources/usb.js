// CAN adapters straight from the browser over WebUSB (Chrome / Edge, on localhost or https). The analyzer
// (page Variabelen) opens them listen-only and never sends; the online simulator (can/simbus.js) opens them
// normally (`{ listen: false }`) and sends with send(frames). The protocols follow the Linux drivers:
//   Microchip CAN BUS Analyzer   drivers/net/can/usb/mcba_usb.c          (no listen-only: the analyzer ACKs frames)
//   candleLight / CANable gs_usb drivers/net/can/usb/gs_usb.c            (listen-only when the firmware offers it)
//   PEAK PCAN-USB                drivers/net/can/usb/peak_usb/pcan_usb.c (listen-only from hardware revision 4)
//   PEAK PCAN-USB FD, Pro FD, X6 drivers/net/can/usb/peak_usb/pcan_usb_fd.c + include/linux/can/dev/peak_canfd.h
// The parsers are plain functions (tested in tests/js with made-up buffers); the drivers around them were not
// tested on hardware except where the README says so.

import { frame } from '../frames.js';

export const USB_DEVICES = [
  { vendorId: 0x04d8, productId: 0x0a30, driver: 'mcba', name: 'Microchip CAN BUS Analyzer' },
  { vendorId: 0x1d50, productId: 0x606f, driver: 'gsusb', name: 'candleLight / CANable (gs_usb)' },
  { vendorId: 0x1209, productId: 0x2323, driver: 'gsusb', name: 'candleLight' },
  { vendorId: 0x1cd2, productId: 0x606f, driver: 'gsusb', name: 'CES CANext FD' },
  { vendorId: 0x16d0, productId: 0x10b8, driver: 'gsusb', name: 'ABE CANdebugger FD' },
  { vendorId: 0x16d0, productId: 0x0f30, driver: 'gsusb', name: 'Xylanta SAINT3' },
  { vendorId: 0x1209, productId: 0xca01, driver: 'gsusb', name: 'CANnectivity' },
  { vendorId: 0x0c72, productId: 0x000c, driver: 'pcan', name: 'PEAK PCAN-USB' },
  { vendorId: 0x0c72, productId: 0x0011, driver: 'pcanfd', name: 'PEAK PCAN-USB Pro FD' },
  { vendorId: 0x0c72, productId: 0x0012, driver: 'pcanfd', name: 'PEAK PCAN-USB FD' },
  { vendorId: 0x0c72, productId: 0x0013, driver: 'pcanfd', name: 'PEAK PCAN-Chip USB' },
  { vendorId: 0x0c72, productId: 0x0014, driver: 'pcanfd', name: 'PEAK PCAN-USB X6' },
];

export const usbAvailable = () => typeof navigator !== 'undefined' && !!navigator.usb && (typeof isSecureContext === 'undefined' || isSecureContext);

const le16 = (b, i) => b[i] | (b[i + 1] << 8);
const le32 = (b, i) => (b[i] | (b[i + 1] << 8) | (b[i + 2] << 16) | (b[i + 3] << 24)) >>> 0;
const be16 = (b, i) => (b[i] << 8) | b[i + 1];
const u32le = (...vals) => { const a = new Uint8Array(vals.length * 4); const dv = new DataView(a.buffer); vals.forEach((v, i) => dv.setUint32(i * 4, v >>> 0, true)); return a; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => Date.now() / 1000;
const bytesOf = (dv) => new Uint8Array(dv.buffer, dv.byteOffset, dv.byteLength);

// ---------------------------------------------------------------- Microchip CAN BUS Analyzer (19-byte messages)
export const MCBA = { RX: 0xe3, TX: 0xa3, TX_RSP: 0xe2, KA_CAN: 0xf5, KA_USB: 0xf7, NOTHING: 0xff, BITRATE: 0xa1, TERMINATION: 0xa8, FW: 0xa9 };

/** Parses a USB transfer of 19-byte messages: CAN frames into `out`, keep-alive data into `st`. */
export function mcbaParse(buf, t, out, st) {
  for (let i = 0; i + 19 <= buf.length; i += 19) {
    const m = buf.subarray(i, i + 19);
    switch (m[0]) {
      case MCBA.RX: {
        const eid = be16(m, 1), sid = be16(m, 3), dlc = m[5];
        if (dlc & 0x40) break; // remote frame
        const len = Math.min(dlc & 0x0f, 8);
        if (sid & 0x08) out.push(frame(t, (((sid & 0xffe0) << 13) | ((sid & 3) << 16) | eid) >>> 0, m.slice(6, 6 + len), true));
        else out.push(frame(t, (sid & 0xffe0) >> 5, m.slice(6, 6 + len), false));
        break;
      }
      case MCBA.KA_CAN:
        Object.assign(st, { txErr: m[1], rxErr: m[2], rxOverflow: m[3], busOff: m[4], kbps: be16(m, 5), rxLost: le16(m, 7) });
        break;
      case MCBA.KA_USB:
        Object.assign(st, { termination: m[1] === 0 ? 'aan' : 'uit', fw: `${m[2]}.${m[3]}` });
        break;
      default: break;
    }
  }
}

export function mcbaCommand(cmd, ...args) {
  const b = new Uint8Array(19);
  b[0] = cmd;
  args.forEach((v, i) => { b[1 + i] = v; });
  return b;
}

/** A frame as the analyzer's 19-byte message: 0xA3 to send it, 0xE3 as the analyzer reports a received one. */
export function mcbaEncode(id, ext, data, cmd = MCBA.TX) {
  const b = new Uint8Array(19);
  b[0] = cmd;
  data = data.slice(0, 8);
  let sid, eid = 0;
  if (ext) { sid = ((id >>> 13) & 0xffe0) | 0x08 | ((id >>> 16) & 3); eid = id & 0xffff; } else sid = (id << 5) & 0xffe0;
  b[1] = eid >> 8; b[2] = eid & 0xff; b[3] = sid >> 8; b[4] = sid & 0xff; b[5] = data.length;
  b.set(data, 6);
  return b;
}

/** For tests: a received frame as the analyzer sends it. */
export const mcbaEncodeRx = (id, ext, data) => mcbaEncode(id, ext, data, MCBA.RX);

// ---------------------------------------------------------------- gs_usb (candleLight)
export const GS = { HOST_FORMAT: 0, BITTIMING: 1, MODE: 2, BT_CONST: 4, DEVICE_CONFIG: 5, LISTEN_ONLY: 1, RX: 0xffffffff };

/** One gs_host_frame (classic CAN, 20 bytes; 24 with a hardware time stamp) → frame or null. */
export function gsParse(buf, t, st) {
  if (buf.length < 20 || le32(buf, 0) !== GS.RX) return null; // TX echo or too short
  const canId = le32(buf, 4);
  if (buf[10] & 0x01) st.lost = (st.lost || 0) + 1; // overflow flag
  if (canId & 0x20000000) { st.errors = (st.errors || 0) + 1; return null; } // error frame
  if (canId & 0x40000000) return null; // remote frame
  const ext = !!(canId & 0x80000000);
  return frame(t, ext ? canId & 0x1fffffff : canId & 0x7ff, buf.slice(12, 12 + Math.min(buf[8], 8)), ext);
}

/** A frame to send (gs_host_frame, 20 bytes). The adapter echoes it with the same echo_id once it is on the bus. */
export function gsEncode(id, ext, data, echoId = 0) {
  const b = new Uint8Array(20);
  const dv = new DataView(b.buffer);
  const len = Math.min(data.length, 8);
  dv.setUint32(0, echoId >>> 0, true);
  dv.setUint32(4, ext ? ((id & 0x1fffffff) | 0x80000000) >>> 0 : id & 0x7ff, true);
  b[8] = len; // can_dlc; channel 0, flags 0
  b.set(data.slice(0, len), 12);
  return b;
}

/** Bit timing for `bitrate` with the controller's limits: sample point 87,5 %, 8..25 time quanta, closest to 16. */
export function bitTiming(fclk, bitrate, c = {}) {
  const lim = { tseg1Min: 1, tseg1Max: 16, tseg2Min: 1, tseg2Max: 8, brpMin: 1, brpMax: 1024, brpInc: 1, ...c };
  let best = null;
  for (let brp = lim.brpMin; brp <= lim.brpMax; brp += lim.brpInc || 1) {
    const tq = fclk / (brp * bitrate);
    if (!Number.isInteger(tq) || tq < 8 || tq > 25) continue;
    const tseg2 = Math.max(lim.tseg2Min, Math.round(tq * 0.125));
    const tseg1 = tq - 1 - tseg2;
    if (tseg1 < lim.tseg1Min || tseg1 > lim.tseg1Max || tseg2 > lim.tseg2Max) continue;
    if (!best || Math.abs(tq - 16) < Math.abs(best.tq - 16)) best = { brp, tq, tseg1, tseg2, sjw: 1 };
  }
  if (!best) throw new Error(`geen bit timing voor ${bitrate / 1000} kbit/s met een klok van ${fclk / 1e6} MHz`);
  return best;
}

// ---------------------------------------------------------------- PCAN-USB (classic)
const PCAN_BTR = { 125000: [0x03, 0x1c], 250000: [0x01, 0x1c], 500000: [0x00, 0x1c], 1000000: [0x00, 0x14] }; // BTR0, BTR1 (SJA1000, 8 MHz)

/** One PCAN-USB message buffer (records) → frames into `out`, status into `st`. */
export function pcanParse(buf, t, out, st) {
  if (buf.length <= 2) return;
  const cnt = buf[1];
  let p = 2, tsIdx = 0;
  const ts = () => { p += tsIdx === 0 ? 2 : 1; tsIdx++; };
  for (let r = 0; r < cnt && p < buf.length; r++) {
    const sl = buf[p++];
    let len = sl & 0x0f;
    if (sl & 0x40) { // internal record: function, number, [time stamp], payload
      if (p + 2 > buf.length) return;
      const f = buf[p], n = buf[p + 1];
      p += 2;
      if (sl & 0x80) ts();
      if (f === 1) { // error
        if (n & 0x02) st.lost = (st.lost || 0) + 1;
        if (n & 0x10) st.busOff = true;
        st.errors = (st.errors || 0) + 1;
      } else if (f === 2) len = 2; // analog
      else if (f === 3) len = 1; // bus load
      else if (f === 5 && p + 2 <= buf.length) { st.rxErr = buf[p]; st.txErr = buf[p + 1]; }
      p += len;
      continue;
    }
    let id, ext, raw;
    if (sl & 0x20) { raw = le32(buf, p); id = raw >>> 3; ext = true; p += 4; } else { raw = le16(buf, p); id = raw >> 5; ext = false; p += 2; }
    ts();
    if (sl & 0x10) continue; // remote frame: no data
    if (p + len > buf.length) return;
    out.push(frame(t, id, buf.slice(p, p + Math.min(len, 8)), ext));
    p += len;
    if (raw & 0x01) p++; // SRR: one private byte follows
  }
}

/** A frame to send: one 64-byte buffer for EP 2 (pcan_usb_encode_msg): type 2, one record, counter in the last byte. */
export function pcanEncode(id, ext, data, counter = 0) {
  const b = new Uint8Array(64);
  const dv = new DataView(b.buffer);
  const len = Math.min(data.length, 8);
  b[0] = 2; // PCAN_USB_MSG_TX_CAN
  b[1] = 1; // one frame
  b[2] = len | (ext ? 0x20 : 0); // status/length
  let p = 3;
  if (ext) { dv.setUint32(p, ((id & 0x1fffffff) << 3) >>> 0, true); p += 4; } else { dv.setUint16(p, (id & 0x7ff) << 5, true); p += 2; }
  b.set(data.slice(0, len), p);
  b[63] = counter & 0xff;
  return b;
}

// ---------------------------------------------------------------- PCAN-USB FD (uCAN)
const UCAN = { RESET: 0x001, NORMAL: 0x002, LISTEN: 0x003, TIMING_SLOW: 0x004, FILTER_STD: 0x008, WR_ERR_CNT: 0x00a, SET_EN_OPTION: 0x00b, CLK_SET: 0x080 };
const FD_LEN = [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64];

/** uCAN records of one USB transfer → frames into `out`, status into `st`. */
export function pcanFdParse(buf, t, out, st) {
  let p = 0;
  while (p + 4 <= buf.length) {
    const size = le16(buf, p);
    if (!size || p + size > buf.length) break;
    const type = le16(buf, p + 2);
    if (type === 0x0001 && size >= 28) { // CAN RX
      const dlc = buf[p + 20] >> 4, flags = le16(buf, p + 22), id = le32(buf, p + 24);
      if (!(flags & 0x01)) { // not a remote frame
        const len = flags & 0x10 ? FD_LEN[dlc] : Math.min(dlc, 8);
        const ext = !!(flags & 0x02);
        out.push(frame(t, ext ? id & 0x1fffffff : id & 0x7ff, buf.slice(p + 28, p + 28 + len), ext));
      }
    } else if (type === 0x0002 && size >= 16) { st.txErr = buf[p + 14]; st.rxErr = buf[p + 15]; st.errors = (st.errors || 0) + 1; }
    else if (type === 0x0003 && size >= 16) { st.busOff = !!(buf[p + 12] & 0x80); st.passive = !!(buf[p + 12] & 0x20); }
    else if (type === 0x0101) st.lost = (st.lost || 0) + 1; // overrun
    p += size;
  }
}

/** A classic frame to send (pucan_tx_msg, 20-byte header, 32-bit aligned), closed by a record of size 0. */
export function pcanFdEncode(id, ext, data, ch = 0) {
  const len = Math.min(data.length, 8);
  const size = (20 + len + 3) & ~3;
  const b = new Uint8Array(size + 4);
  const dv = new DataView(b.buffer);
  dv.setUint16(0, size, true);
  dv.setUint16(2, 0x1000, true); // PUCAN_MSG_CAN_TX
  b[12] = (ch & 0x0f) | (len << 4); // channel_dlc
  dv.setUint16(14, ext ? 0x02 : 0, true); // flags: PUCAN_MSG_EXT_ID
  dv.setUint32(16, ext ? id & 0x1fffffff : id & 0x7ff, true);
  b.set(data.slice(0, len), 20);
  return b;
}

const op = (code, ch = 0) => [((ch << 12) | (code & 0x3ff)) & 0xff, ((ch << 12) | (code & 0x3ff)) >> 8];

/** uCAN command records (8 bytes each) in transfers of at most 64 bytes, each closed by "end of collection". */
export function ucanPackets(records) {
  const out = [];
  for (let i = 0; i < records.length; i += 7) {
    const part = records.slice(i, i + 7);
    const b = new Uint8Array(part.length * 8 + 8).fill(0xff);
    part.forEach((r, k) => { b.fill(0, k * 8, k * 8 + 8); b.set(r, k * 8); });
    out.push(b);
  }
  return out;
}

// ---------------------------------------------------------------- the source
const BULK = 'bulk';

function endpoints(device) {
  for (const iface of device.configuration.interfaces) {
    const alt = iface.alternate || iface.alternates[0];
    const eps = alt.endpoints.filter((e) => e.type === BULK);
    const ins = eps.filter((e) => e.direction === 'in'), outs = eps.filter((e) => e.direction === 'out');
    if (ins.length && outs.length) return { iface: iface.interfaceNumber, ins: ins.map((e) => e.endpointNumber), outs: outs.map((e) => e.endpointNumber) };
  }
  throw new Error('geen bulk-endpoints gevonden');
}

const supported = (d) => USB_DEVICES.find((u) => u.vendorId === d.vendorId && u.productId === d.productId);
const usbApi = () => (typeof navigator !== 'undefined' ? navigator.usb : null);

/** Rejects when `promise` takes longer than `ms`: a USB transfer that the adapter never takes would wait forever. */
function withTimeout(promise, ms, what) {
  let timer;
  const late = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${what}: de adapter antwoordt niet (time-out)`)), ms); });
  return Promise.race([promise, late]).finally(() => clearTimeout(timer));
}

export class UsbSource {
  /** Longest wait for one USB step or transfer. */
  static TIMEOUT_MS = 2000;

  /** opts.listen (default true): listen-only where the adapter can; false for the simulator, which sends. */
  constructor(device, bitrate, opts = {}) {
    this.kind = 'usb';
    this.device = device;
    this.bitrate = bitrate;
    this.listen = opts.listen !== false;
    this.batch = 8; // frames per send() call, all on the way at once (Linux: 10 to 20 transfers in flight)
    this.txCount = 0;
    this.inCount = 0; // USB transfers received (diagnosis: does the adapter talk at all?)
    this.info = USB_DEVICES.find((d) => d.vendorId === device.vendorId && d.productId === device.productId)
      || { name: device.productName || 'USB-adapter', driver: null };
    this.st = {};
    this.closed = false;
    this.lostSeen = 0;
  }

  get label() { return this.info.name; }

  static async request(bitrate, opts) {
    const device = await navigator.usb.requestDevice({ filters: USB_DEVICES.map(({ vendorId, productId }) => ({ vendorId, productId })) });
    return new UsbSource(device, bitrate, opts);
  }

  /** An adapter this site may already use (no chooser, no click needed): the remembered one, else the first. */
  static async allowed(remember, bitrate, opts) {
    const list = (await navigator.usb.getDevices()).filter(supported);
    const same = (d) => remember && d.vendorId === remember.vendorId && d.productId === remember.productId
      && (!remember.serial || d.serialNumber === remember.serial);
    const device = list.find(same) || list[0];
    return device ? new UsbSource(device, bitrate, opts) : null;
  }

  /** What to remember to find this adapter again. */
  get identity() { return { vendorId: this.device.vendorId, productId: this.device.productId, serial: this.device.serialNumber || '' }; }

  /** Sends frames ({id, ext, data}), one USB transfer each. The transfers are started together and finish in
   *  order: waiting for each one before the next limits a sending simulator to one frame per USB round trip. */
  async send(frames) {
    await Promise.all(frames.map((f) => this.out(this.txEp, this.encode(f))));
  }

  /** Opens and sets up the adapter. Every step shows in the status, so a step that hangs can be seen. Like the
   *  Linux drivers, reading starts before the first command: the Microchip firmware only takes a new command when
   *  its own messages (keep-alives, answers) can go out to the host. */
  async open(sink) {
    this.sink = sink;
    const d = this.device;
    const drv = this.info.driver;
    if (!drv) throw new Error(`${this.info.name} wordt niet ondersteund`);
    const step = (t) => sink.status(`${this.info.name}: ${t}`);
    try {
      step('USB openen…');
      await withTimeout(d.open(), UsbSource.TIMEOUT_MS, 'USB openen');
      if (!d.configuration) await withTimeout(d.selectConfiguration(1), UsbSource.TIMEOUT_MS, 'configuratie kiezen');
      this.ep = endpoints(d);
      step(`interface ${this.ep.iface} claimen (bulk in ${this.ep.ins.join('/')}, uit ${this.ep.outs.join('/')})…`);
      await withTimeout(d.claimInterface(this.ep.iface), UsbSource.TIMEOUT_MS, 'interface claimen');
    } catch (e) {
      await this.close();
      const win = typeof navigator !== 'undefined' && /Windows/.test(navigator.userAgent || '');
      throw new Error(`USB openen mislukt: ${e.message || e}.` + (win
        ? ' Op Windows: sluit de software van de fabrikant (die houdt de adapter vast) en kijk in chrome://device-log welke driver Windows gebruikt; de browser kan er enkel bij met WinUSB.'
        : ' Heeft een ander programma of tabblad de adapter open?'));
    }
    this.onDisconnect = (e) => { if (e.device === d && !this.closed) { this.closed = true; sink.closed('De USB-adapter is losgekoppeld.'); } };
    usbApi()?.addEventListener('disconnect', this.onDisconnect);
    const ep = this.ep;
    [this.inEp, this.outEp, this.inLen] = { mcba: [ep.ins[0], ep.outs[0], 64], gsusb: [ep.ins[0], ep.outs[0], 64],
      pcan: [2, 1, 64], pcanfd: [2, 1, 2048] }[drv]; // PEAK: commands out 1, messages in 0x82
    this.parse = {
      mcba: (buf, t, out) => mcbaParse(buf, t, out, this.st),
      gsusb: (buf, t, out) => { const f = gsParse(buf, t, this.st); if (f) out.push(f); },
      pcan: (buf, t, out) => pcanParse(buf, t, out, this.st),
      pcanfd: (buf, t, out) => pcanFdParse(buf, t, out, this.st),
    }[drv];
    this.readLoop();
    let note = '';
    try {
      step(`instellen op ${this.bitrate / 1000} kbit/s…`);
      if (drv === 'mcba') note = await this.startMcba();
      else if (drv === 'gsusb') note = await this.startGs();
      else if (drv === 'pcan') note = await this.startPcan();
      else note = await this.startPcanFd();
    } catch (e) {
      await this.close();
      throw new Error(`${e.message || e} (${this.inCount ? `${this.inCount} USB-berichten ontvangen` : 'nog niets ontvangen van de adapter'})`);
    }
    sink.status(`${this.info.name} · ${this.bitrate / 1000} kbit/s${note ? ' · ' + note : ''}`);
  }

  async out(epNum, data) {
    const r = await withTimeout(this.device.transferOut(epNum, data), UsbSource.TIMEOUT_MS, 'USB-schrijven');
    if (r.status !== 'ok') throw new Error(`USB-schrijven: ${r.status}`);
  }

  async startMcba() {
    await this.out(this.outEp, mcbaCommand(MCBA.BITRATE, (this.bitrate / 1000) >> 8, (this.bitrate / 1000) & 0xff));
    await this.out(this.outEp, mcbaCommand(MCBA.FW, 1));
    await this.out(this.outEp, mcbaCommand(MCBA.FW, 2));
    this.txEp = this.outEp;
    this.encode = (f) => mcbaEncode(f.id, f.ext, f.data);
    return this.listen ? 'geen listen-only: de analyzer bevestigt frames (ACK), maar zendt niets' : '';
  }

  async ctrlOut(request, value, data) {
    const r = await withTimeout(this.device.controlTransferOut({ requestType: 'vendor', recipient: 'interface', request, value, index: this.ep.iface }, data),
      UsbSource.TIMEOUT_MS, `USB-verzoek ${request}`);
    if (r.status !== 'ok') throw new Error(`USB-verzoek ${request}: ${r.status}`);
  }

  async ctrlIn(request, value, length) {
    const r = await withTimeout(this.device.controlTransferIn({ requestType: 'vendor', recipient: 'interface', request, value, index: this.ep.iface }, length),
      UsbSource.TIMEOUT_MS, `USB-verzoek ${request}`);
    if (r.status !== 'ok') throw new Error(`USB-verzoek ${request}: ${r.status}`);
    return bytesOf(r.data);
  }

  async startGs() {
    await this.ctrlOut(GS.HOST_FORMAT, 1, u32le(0x0000beef));
    const cfg = await this.ctrlIn(GS.DEVICE_CONFIG, 1, 12);
    const bt = await this.ctrlIn(GS.BT_CONST, 0, 40);
    const feature = le32(bt, 0), fclk = le32(bt, 4);
    const tm = bitTiming(fclk, this.bitrate, { tseg1Min: le32(bt, 8), tseg1Max: le32(bt, 12), tseg2Min: le32(bt, 16), tseg2Max: le32(bt, 20),
      brpMin: le32(bt, 28), brpMax: le32(bt, 32), brpInc: le32(bt, 36) || 1 });
    await this.ctrlOut(GS.MODE, 0, u32le(0, 0)); // reset
    await this.ctrlOut(GS.BITTIMING, 0, u32le(1, tm.tseg1 - 1, tm.tseg2, tm.sjw, tm.brp)); // prop_seg, phase_seg1, phase_seg2, sjw, brp
    const listen = this.listen && !!(feature & GS.LISTEN_ONLY);
    await this.ctrlOut(GS.MODE, 0, u32le(1, listen ? GS.LISTEN_ONLY : 0)); // start
    this.stopCmd = () => this.ctrlOut(GS.MODE, 0, u32le(0, 0));
    this.txEp = this.outEp;
    this.encode = (f) => gsEncode(f.id, f.ext, f.data, this.txCount++ % 10);
    const mode = !this.listen ? '' : listen ? ', listen-only' : ', geen listen-only in deze firmware: de adapter bevestigt frames (ACK)';
    return `${cfg[3] + 1} kanaal/kanalen, klok ${fclk / 1e6} MHz${mode}`;
  }

  async startPcan() {
    const cmd = (f, n, args = []) => { const b = new Uint8Array(16); b[0] = f; b[1] = n; b.set(args, 2); return this.out(1, b); };
    const btr = PCAN_BTR[this.bitrate];
    if (!btr) throw new Error(`PCAN-USB: ${this.bitrate / 1000} kbit/s niet ondersteund (125, 250, 500, 1000)`);
    await cmd(3, 2, [0]); // bus off
    await cmd(9, 2, [0, 1]); // SJA1000 in init mode
    await cmd(1, 2, [btr[1], btr[0]]); // bitrate: BTR1, BTR0
    await cmd(11, 2, [0x06]); // report rx/tx error counters
    const rev = this.device.deviceVersionMajor;
    const silent = this.listen && rev > 3;
    if (silent) await cmd(3, 3, [1]); // listen-only
    await cmd(10, 2, [0]); // no external 5 V
    await cmd(3, 2, [1]); // bus on
    await sleep(10);
    this.stopCmd = () => cmd(3, 2, [0]);
    this.txEp = 2; // messages out
    this.encode = (f) => pcanEncode(f.id, f.ext, f.data, this.txCount++);
    if (!this.listen) return '';
    return silent ? 'listen-only' : `hardware-revisie ${rev}: geen listen-only, de adapter bevestigt frames (ACK)`;
  }

  async startPcanFd() {
    const send = async (records) => { for (const b of ucanPackets(records)) await this.out(1, b); };
    const drvLoaded = (on) => this.device.controlTransferOut({ requestType: 'vendor', recipient: 'other', request: 2, value: 5, index: 0 },
      Uint8Array.from([0, on ? 1 : 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]));
    await drvLoaded(true);
    const tm = bitTiming(80e6, this.bitrate, { tseg1Max: 256, tseg2Max: 128 });
    await send([
      [...op(UCAN.RESET), 0, 0, 0, 0, 0, 0],
      [...op(UCAN.CLK_SET), 0, 0, 0, 0, 0, 0], // 80 MHz
      [...op(UCAN.TIMING_SLOW), 96, tm.sjw - 1, tm.tseg2 - 1, tm.tseg1 - 1, (tm.brp - 1) & 0xff, (tm.brp - 1) >> 8],
    ]);
    const rows = [];
    for (let i = 0; i < 64; i++) rows.push([...op(UCAN.FILTER_STD), i, 0, 0xff, 0xff, 0xff, 0xff]); // accept every 11-bit id
    await send(rows);
    await send([
      [...op(UCAN.SET_EN_OPTION), 0x01, 0x00, 0, 0, 0x00, 0x80], // error messages + USB calibration
      [...op(UCAN.WR_ERR_CNT), 0x00, 0xc0, 0, 0, 0, 0], // reset both error counters
      [...op(this.listen ? UCAN.LISTEN : UCAN.NORMAL), 0, 0, 0, 0, 0, 0], // operational: listen-only or normal
    ]);
    this.stopCmd = async () => { await send([[...op(UCAN.RESET), 0, 0, 0, 0, 0, 0]]); await drvLoaded(false); };
    this.txEp = 2; // messages out (PCAN_USBPRO_EP_MSGOUT_0)
    this.encode = (f) => pcanFdEncode(f.id, f.ext, f.data);
    return this.listen ? 'listen-only' : '';
  }

  async readLoop() {
    const d = this.device;
    const pending = [];
    const next = () => {
      const p = d.transferIn(this.inEp, this.inLen);
      p.catch(() => {}); // on close every waiting read rejects; only the awaited one is handled below
      return p;
    };
    try {
      for (let i = 0; i < 4; i++) pending.push(next());
      while (!this.closed) {
        const r = await pending.shift();
        if (this.closed) break;
        pending.push(next());
        if (r.status === 'stall') { await d.clearHalt('in', this.inEp); continue; }
        if (r.status !== 'ok' || !r.data || !r.data.byteLength) continue;
        this.inCount++;
        this.sink.activity?.();
        const out = [];
        this.parse(bytesOf(r.data), now(), out);
        if (out.length) this.sink.frames(out);
        const lost = (this.st.lost || 0) + (this.st.rxLost || 0);
        if (lost > this.lostSeen) { this.sink.lost?.(lost - this.lostSeen); this.lostSeen = lost; }
      }
    } catch (e) {
      if (!this.closed) { this.closed = true; this.sink.closed(`USB: ${e.message || e}`); }
    }
  }

  async close() {
    this.closed = true;
    if (this.onDisconnect) usbApi()?.removeEventListener('disconnect', this.onDisconnect);
    try { await this.stopCmd?.(); } catch { /* already gone */ }
    try { await withTimeout(this.device.close(), Math.min(1000, UsbSource.TIMEOUT_MS), 'USB sluiten'); } catch { /* already closed, or stuck */ }
  }
}
