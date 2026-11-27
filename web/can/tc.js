// Process data of the task controller (ISO 11783-10, PGN 0xCB00) as variables per implement:
//
//   80 · Machine control
//   └ Procesdata
//      ├ Toestel (element 0)
//      │  ├ Actual Work State          aan
//      │  └ Werkbreedte                6 000 mm     (DPT, a constant from the DDOP)
//      └ Sectie 1 (element 3)
//         ├ Actual Volume Per Area Application Rate      actual, from the implement
//         └ … · setpunt                                  setpoint, from the task controller
//
// The DDOP is caught when the implement sends it to the TC (device descriptor, object pool transfer 0x61) or
// loaded from a file; without it the elements are numbered and the DDIs named from ddi.js.
// Process data byte 1: command (bits 1-4) and element number low nibble (bits 5-8); byte 2: element number high
// byte; bytes 3-4: DDI; bytes 5-8: value (int32).

import { parseDdop, DET_TYPES, NONE } from './ddop.js';
import { ddiInfo, ddiText } from './ddi.js';

const CMD = { CAPABILITIES: 0x0, DEVICE_DESCRIPTOR: 0x1, REQUEST_VALUE: 0x2, VALUE: 0x3, SET_ACK: 0xa, ACK: 0xd, TC_STATUS: 0xe, CLIENT_TASK: 0xf };

export class TcPlugin {
  constructor() {
    this.pgns = new Set([0xcb00]);
    this.reset();
  }

  reset() {
    this.tcs = new Set(); // addresses that act as task controller
  }

  isTc(model, sa) {
    if (this.tcs.has(sa)) return true;
    return model.devices.get(sa)?.fields?.function === 130;
  }

  /** Per client device: { pool: Uint8Array chunks, objects, byNumber, byId, src } */
  state(dev) {
    if (!dev.tc) dev.tc = { chunks: [], objects: null, byId: new Map(), byNumber: new Map(), src: '', error: '' };
    return dev.tc;
  }

  group(model, dev) {
    return dev.group('TC', 'Procesdata', () => [...dev.groups.get('TC').items.values()].sort((a, b) => (a.number ?? -1) - (b.number ?? -1)));
  }

  element(model, dev, number) {
    const g = this.group(model, dev);
    const key = `E${number}`;
    let el = g.items.get(key);
    if (!el) {
      const det = this.state(dev).byNumber.get(number);
      el = new (g.constructor)(g, key, det ? `${det.designator || DET_TYPES[det.type] || 'element'} (element ${number})` : `Element ${number}`);
      el.number = number;
      g.items.set(key, el);
      model.rev++;
    }
    return el;
  }

  message(model, m, dev) {
    const d = m.data;
    if (!d.length) return;
    const cmd = d[0] & 0x0f;
    switch (cmd) {
      case CMD.TC_STATUS: {
        if (d.length < 8) return;
        this.tcs.add(m.sa);
        const g = dev.group('TCSTAT', 'Taakcontroller');
        g.v('task', 'Taak actief').setText(d[4] & 0x01 ? 'ja' : 'nee', m.t);
        g.v('client', 'Bezig met adres').setText(d[5] === 0xff ? '—' : d[5].toString(16).toUpperCase().padStart(2, '0'), m.t);
        break;
      }
      case CMD.DEVICE_DESCRIPTOR: this.descriptor(model, m, dev); break;
      case CMD.VALUE:
      case CMD.SET_ACK: {
        if (d.length < 8) return;
        const number = (d[0] >> 4) | (d[1] << 4);
        const ddi = d[2] | (d[3] << 8);
        const raw = (d[4] | (d[5] << 8) | (d[6] << 16) | (d[7] << 24)) | 0;
        const fromTc = this.isTc(model, m.sa); // a value from the TC is a setpoint for the client it is sent to
        const client = fromTc ? model.devices.get(m.da) : dev;
        if (!client) return;
        this.value(model, client, number, ddi, raw, fromTc, m.t);
        break;
      }
      default: break;
    }
  }

  descriptor(model, m, dev) {
    const d = m.data;
    const sub = d[0] >> 4;
    if (this.isTc(model, m.sa)) return; // answers of the TC
    const st = this.state(dev);
    if (sub === 0x4) st.chunks = []; // request object-pool transfer: a new pool follows
    else if (sub === 0x6) { // object-pool transfer
      st.chunks.push(d.slice(1));
      const all = new Uint8Array(st.chunks.reduce((n, c) => n + c.length, 0));
      let at = 0;
      for (const c of st.chunks) { all.set(c, at); at += c.length; }
      this.setDdop(model, dev, all, 'bus', m.t);
    } else if (sub === 0xa) { st.chunks = []; } // delete object pool
  }

  /** Builds the process data tree of `dev` from DDOP bytes ('bus' or a file name as source). */
  setDdop(model, dev, bytes, src, t = 0) {
    const st = this.state(dev);
    let objects;
    try { objects = parseDdop(bytes); } catch (e) { st.error = e.message; return false; }
    st.objects = objects;
    st.bytes = bytes;
    st.src = src;
    st.error = '';
    st.byId = new Map(objects.map((o) => [o.id, o]));
    st.byNumber = new Map(objects.filter((o) => o.table === 'DET').map((o) => [o.number, o]));
    const g = this.group(model, dev);
    // rename numbered elements and add the DDOP's process data and properties
    for (const det of st.byNumber.values()) {
      const el = this.element(model, dev, det.number);
      el.label = `${det.designator || DET_TYPES[det.type] || 'element'} (element ${det.number})`;
      el.det = det;
      for (const cid of det.children) {
        const o = st.byId.get(cid);
        if (!o) continue;
        const def = this.def(st, o.ddi, o);
        const v = el.v(String(o.ddi), def.label, def);
        v.name = def.label;
        v.def = def;
        v.unit = def.unit;
        if (o.table === 'DPT') this.show(v, o.value, def, t);
      }
    }
    const dvc = objects.find((o) => o.table === 'DVC');
    if (dvc) {
      const I = g;
      I.v('ddop', 'DDOP').setText(`${dvc.designator} · software ${dvc.softwareVersion} · ${objects.length} objecten (${src === 'bus' ? 'van de bus' : src})`, t);
    }
    model.rev++;
    return true;
  }

  /** Definition of a DDI on an element: designator and presentation from the DDOP, else the DDI table. */
  def(st, ddi, obj = null) {
    const info = ddiInfo(ddi);
    const dvp = obj && obj.presentation !== NONE ? st.byId.get(obj.presentation) : null;
    const label = obj?.designator || info.name;
    if (dvp && dvp.table === 'DVP') {
      return { label, ddi, ddiName: info.name, unit: dvp.unit, offset: dvp.offset, scaleDvp: Number(dvp.scale.toPrecision(7)), decimals: dvp.decimals,
        scale: 10 ** -dvp.decimals, kind: info.kind, source: `DDOP ${obj.table} ${obj.id}, weergave DVP ${dvp.id}` };
    }
    return { label, ddi, ddiName: info.name, unit: info.unit, res: info.res, scale: info.res, kind: info.kind,
      source: obj ? `DDOP ${obj.table} ${obj.id}` : 'DDI-tabel (geen DDOP)' };
  }

  show(v, raw, def, t) {
    const txt = ddiText(def, raw);
    if (txt !== null) { v.set({ value: raw, text: txt, status: 'ok', raw }, t); return; }
    const value = def.scaleDvp !== undefined ? (raw + def.offset) * def.scaleDvp : raw * (def.res ?? 1);
    v.set({ value, text: fmt(value, def), status: 'ok', raw }, t);
  }

  value(model, dev, number, ddi, raw, setpoint, t) {
    const st = this.state(dev);
    const el = this.element(model, dev, number);
    const obj = el.det ? el.det.children.map((c) => st.byId.get(c)).find((o) => o && o.ddi === ddi) : null;
    const def = this.def(st, ddi, obj);
    const key = setpoint ? `${ddi}.sp` : String(ddi);
    const name = setpoint ? `${def.label} · setpunt` : def.label;
    const v = el.v(key, name, def);
    this.show(v, raw, def, t);
  }
}

function fmt(v, def) {
  const dec = def.decimals ?? (def.res && def.res < 1 ? Math.min(7, Math.ceil(-Math.log10(def.res) - 1e-9)) : 0);
  return v.toLocaleString('nl-BE', { minimumFractionDigits: dec, maximumFractionDigits: dec, useGrouping: Math.abs(v) >= 1e4 });
}
