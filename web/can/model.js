// What the analyzer knows about the bus: devices (control functions), their messages and the variables in
// those messages, as a tree that the page browses like an OPC UA address space.
//
//   Bus
//   └ Device (source address, recognised by its NAME)
//      ├ Identiteit   NAME fields, software, ECU identification
//      ├ Berichten    one node per PGN (or DBC message) → its signals as variables
//      ├ …            groups added by plug-ins (process data of the task controller)
//      └ Diagnose     DM1: lamps and active trouble codes
//
// Frames go in with push(); every node has a stable `id`, a `label`, a `kind` and kids().

import { splitId, PGN, hex, frameBits } from './frames.js';
import { Transport } from './transport.js';
import { messageDef, nameFromBytes, nameDecode, functionName, manufacturerName, INDUSTRY, decodeDm, FMI } from './isobus-db.js';
import { decodeSignal, fmtNum } from './signals.js';

const HIST = 20000; // points per watched variable

export class Var {
  constructor(parent, key, name, def = null) {
    this.parent = parent;
    this.kind = 'var';
    this.key = key;
    this.name = name;
    this.def = def;
    this.unit = def?.unit || '';
    this.value = null;
    this.text = '—';
    this.status = 'none';
    this.raw = null;
    this.t = 0;
    this.min = null;
    this.max = null;
    this.n = 0;
    this.hist = null;
  }

  get id() { return `${this.parent.id}/${this.key}`; }
  get label() { return this.name; }
  get path() { return `${this.parent.path}/${this.name}`; }
  /** Key that survives a reload and an address change (for the watch list). */
  get stableKey() { return `${this.parent.stableKey}/${this.key}`; }
  get device() { return this.parent.device; }
  kids() { return []; }

  /** r: result of decodeSignal, or { value, text, status }. */
  set(r, t) {
    this.value = r.value;
    this.text = r.text;
    this.status = r.status;
    this.raw = r.raw ?? null;
    this.t = t;
    this.n++;
    if (typeof r.value === 'number' && r.status === 'ok') {
      if (this.min === null || r.value < this.min) this.min = r.value;
      if (this.max === null || r.value > this.max) this.max = r.value;
      if (this.hist) {
        const h = this.hist;
        h.t[h.i] = t; h.v[h.i] = r.value;
        h.i = (h.i + 1) % HIST;
        if (h.n < HIST) h.n++;
      }
    }
  }

  setText(text, t) { this.set({ value: text, text, status: text === '' ? 'na' : 'ok' }, t); }
  setNum(value, t, sig = this.def) { this.set({ value, text: fmtNum(value, sig), status: 'ok' }, t); }

  track(on) {
    if (on && !this.hist) this.hist = { t: new Float64Array(HIST), v: new Float64Array(HIST), n: 0, i: 0 };
    if (!on) this.hist = null;
  }

  /** History as two arrays in time order. */
  series() {
    const h = this.hist;
    if (!h) return { t: [], v: [] };
    const t = new Array(h.n), v = new Array(h.n);
    const start = h.n < HIST ? 0 : h.i;
    for (let k = 0; k < h.n; k++) { const j = (start + k) % HIST; t[k] = h.t[j]; v[k] = h.v[j]; }
    return { t, v };
  }
}

export class Group {
  constructor(parent, key, label, kidsFn = null) {
    this.parent = parent;
    this.kind = 'group';
    this.key = key;
    this.label = label;
    this.items = new Map();
    this.kidsFn = kidsFn;
  }

  get id() { return `${this.parent.id}/${this.key}`; }
  get path() { return `${this.parent.path}/${this.key}`; }
  get stableKey() { return `${this.parent.stableKey}/${this.key}`; }
  get device() { return this.parent.device; }
  kids() { return this.kidsFn ? this.kidsFn() : [...this.items.values()]; }

  /** A variable in this group, created on first use. */
  v(key, name = key, def = null) {
    let x = this.items.get(key);
    if (!x) { x = new Var(this, key, name, def); this.items.set(key, x); this.device?.model?.added(x); }
    return x;
  }
}

export class Msg {
  constructor(dev, key, def, src) {
    this.parent = dev;
    this.kind = 'msg';
    this.key = key;
    this.def = def;
    this.src = src; // 'iso' or 'dbc:<file name>'
    this.name = def.name;
    this.pgn = def.pgn ?? null;
    this.canId = null;
    this.count = 0;
    this.t = 0;
    this.period = 0;
    this.data = null;
    this.da = null;
    this.via = '';
    this.vars = def.signals.map((s) => new Var(this, s.name, s.name, s));
  }

  get id() { return `${this.parent.id}/${this.key}`; }
  get label() { return this.name; }
  get path() { return `${this.parent.path}/${this.key}`; }
  get stableKey() { return `${this.parent.stableKey}/${this.key}`; }
  get device() { return this.parent; }
  kids() { return this.vars; }

  update(m) {
    if (this.count && m.t > this.t) {
      const dt = m.t - this.t;
      this.period = this.period ? this.period * 0.8 + dt * 0.2 : dt;
    }
    this.count++;
    this.t = m.t;
    this.data = m.data;
    this.da = m.da;
    this.via = m.via;
    if (m.id !== undefined) this.canId = m.id;
    const sigs = this.def.signals;
    let muxVal;
    for (let i = 0; i < sigs.length; i++) {
      const s = sigs[i];
      if (s.mux) {
        if (muxVal === undefined) {
          const ms = sigs.find((x) => x.isMux);
          muxVal = ms ? decodeSignal(m.data, ms).raw : null;
        }
        if (muxVal === null || Number(muxVal) !== s.mux.value) continue;
      }
      this.vars[i].set(decodeSignal(m.data, s), m.t);
    }
  }
}

export class Device {
  constructor(model, sa) {
    this.model = model;
    this.kind = 'device';
    this.sa = sa;
    this.serial = ++model.serial;
    this.nameBig = null;
    this.fields = null;
    this.msgs = new Map();
    this.groups = new Map(); // extra groups from plug-ins (key → Group)
    this.lost = false; // another control function claimed this address
    this.firstT = 0;
    this.lastT = 0;
    this.frames = 0;
    this.ident = new Group(this, 'Identiteit', 'Identiteit');
    this.berichten = new Group(this, 'Berichten', 'Berichten', () => [...this.msgs.values()].sort((a, b) => a.key.localeCompare(b.key)));
    this.diag = new Group(this, 'Diagnose', 'Diagnose');
    this.ident.v('Adres').setText(hex(sa), 0);
  }

  get id() { return `d${this.serial}`; }
  get device() { return this; }
  get path() { return this.sa === null ? 'STD' : hex(this.sa); }
  get nameHex() { return this.nameBig === null ? null : this.nameBig.toString(16).toUpperCase().padStart(16, '0'); }
  get stableKey() { return this.nameHex ? `N${this.nameHex}` : this.sa === null ? 'STD' : `A${hex(this.sa)}`; }
  get function() { return this.fields ? functionName(this.fields.function) : null; }

  get label() {
    if (this.sa === null) return '11-bit berichten';
    const fn = this.fields ? functionName(this.fields.function) : 'geen Address Claimed gezien';
    return `${hex(this.sa)} · ${fn}${this.lost ? ' (adres kwijt)' : ''}`;
  }

  get sub() { return this.fields ? manufacturerName(this.fields.manufacturer) : ''; }

  kids() {
    const out = [];
    if (this.sa !== null) out.push(this.ident);
    out.push(this.berichten, ...this.groups.values());
    if (this.diag.items.size) out.push(this.diag);
    return out;
  }

  group(key, label, kidsFn) {
    let g = this.groups.get(key);
    if (!g) { g = new Group(this, key, label, kidsFn); this.groups.set(key, g); this.model.rev++; }
    return g;
  }

  setName(n, t) {
    this.nameBig = n;
    this.fields = nameDecode(n);
    const f = this.fields, I = this.ident;
    I.v('NAME').setText(this.nameHex, t);
    I.v('Functie').set({ value: f.function, text: `${functionName(f.function)} (${f.function})`, status: 'ok' }, t);
    I.v('Fabrikant').set({ value: f.manufacturer, text: `${manufacturerName(f.manufacturer)} (${f.manufacturer})`, status: 'ok' }, t);
    I.v('Function instance').setNum(f.functionInstance, t);
    I.v('ECU instance').setNum(f.ecuInstance, t);
    I.v('Device class').setNum(f.deviceClass, t);
    I.v('Device class instance').setNum(f.deviceClassInstance, t);
    I.v('Industry group').set({ value: f.industryGroup, text: `${INDUSTRY[f.industryGroup] ?? '?'} (${f.industryGroup})`, status: 'ok' }, t);
    I.v('Identity number').setNum(f.identity, t);
    I.v('Self-configurable').setText(f.selfConfigurable ? 'ja' : 'nee', t);
    this.model.rev++;
  }
}

export class Model {
  constructor() {
    this.serial = 0;
    this.plugins = []; // { pgns: Set, message(model, m, dev), reset?() }
    this.dbcs = []; // { name, enabled, db } (dbc.js)
    this.watch = new Set(); // Var
    this.pendingWatch = new Set(); // stable keys waiting for their variable
    this.onWatch = null;
    this.reset();
  }

  reset() {
    this.transport = new Transport((m) => this.message(m));
    this.devices = new Map(); // sa → Device
    this.all = []; // every device, also those that moved
    this.std = null; // pseudo-device for 11-bit frames
    for (const v of this.watch) this.pendingWatch.add(v.stableKey);
    this.watch.clear();
    this.rev = 1; // structure version (new nodes)
    this.now = 0;
    this.stats = { frames: 0, ext: 0, std: 0, tx: 0, errors: 0, lost: 0, rate: 0, load: 0, bitrate: this.stats?.bitrate || 250000,
      t0: null, rateT: null, rateFrames: 0, rateBits: 0 };
    this.bus = new BusNode(this);
    for (const p of this.plugins) p.reset?.();
  }

  /** Root of the tree. */
  get root() { return this.bus; }

  /** A batch of frames from a source. */
  push(frames) {
    for (const f of frames) this.frame(f);
  }

  frame(f) {
    const s = this.stats;
    s.frames++;
    if (f.dir === 'tx') s.tx++;
    if (s.t0 === null) { s.t0 = f.t; s.rateT = f.t; }
    if (f.t > this.now) this.now = f.t;
    s.rateBits += frameBits(f);
    s.rateFrames++;
    if (this.now - s.rateT >= 1) this.rates(this.now);
    if (!f.ext) { s.std++; this.standard(f); return; }
    s.ext++;
    const { prio, pgn, sa, da } = splitId(f.id);
    const dev = this.device(sa, f.t);
    dev.frames++;
    dev.lastT = f.t;
    this.transport.push(f.t, prio, pgn, sa, da, f.data, f.id);
  }

  /** Frames per second and bus load since the last call (t: time in the frames' clock). */
  rates(t) {
    const s = this.stats;
    if (s.rateT === null) return;
    const dt = t - s.rateT;
    if (dt <= 0) return;
    s.rate = Math.round(s.rateFrames / dt);
    s.load = Math.min(100, (100 * s.rateBits) / dt / s.bitrate);
    s.rateT = t; s.rateFrames = 0; s.rateBits = 0;
    this.bus.ratesVar.setNum(s.rate, t);
    this.bus.loadVar.setNum(Math.round(s.load * 10) / 10, t);
  }

  device(sa, t = this.now) {
    let d = this.devices.get(sa);
    if (!d) {
      d = new Device(this, sa);
      d.firstT = t;
      this.devices.set(sa, d);
      this.all.push(d);
      this.rev++;
    }
    return d;
  }

  added(v) {
    this.rev++;
    if (this.pendingWatch.has(v.stableKey)) { this.pendingWatch.delete(v.stableKey); this.setWatch(v, true); }
  }

  setWatch(v, on) {
    if (on) { this.watch.add(v); v.track(true); } else { this.watch.delete(v); v.track(false); }
    this.onWatch?.();
  }

  /** DBC definition for a frame: exact identifier first, then the PGN of a J1939 message. */
  dbcFind(id, ext, pgn) {
    for (const d of this.dbcs) {
      if (!d.enabled) continue;
      const m = id !== undefined ? d.db.byId.get(`${ext ? 'x' : 's'}${id}`) : undefined;
      if (m) return { def: m, src: `dbc:${d.name}` };
    }
    if (pgn === undefined) return null;
    for (const d of this.dbcs) {
      if (!d.enabled) continue;
      const m = d.db.byPgn.get(pgn);
      if (m) return { def: m, src: `dbc:${d.name}` };
    }
    return null;
  }

  /** After loading or switching a DBC: give every message its (new) definition, keeping watched signals. */
  refreshDefs() {
    for (const dev of this.all) {
      for (const msg of dev.msgs.values()) {
        const found = dev.sa === null ? this.dbcFind(msg.canId, false) : this.dbcFind(msg.via ? undefined : msg.canId ?? undefined, true, msg.pgn);
        const def = found ? found.def : dev.sa === null ? { name: `ID ${msg.key}`, signals: [] } : messageDef(msg.pgn);
        const src = found ? found.src : dev.sa === null ? 'raw' : 'iso';
        if (def === msg.def && src === msg.src) continue;
        for (const v of msg.vars) if (this.watch.has(v)) { this.watch.delete(v); this.pendingWatch.add(v.stableKey); }
        msg.def = def;
        msg.src = src;
        msg.name = def.name;
        msg.vars = def.signals.map((s) => new Var(msg, s.name, s.name, s));
        for (const v of msg.vars) this.added(v);
        if (msg.data) { const n = msg.count; msg.update({ t: msg.t, data: msg.data, da: msg.da, via: msg.via }); msg.count = n; }
      }
    }
    this.rev++;
    this.onWatch?.();
  }

  standard(f) {
    if (!this.std) { this.std = new Device(this, null); this.all.push(this.std); this.rev++; }
    const key = hex(f.id, 3);
    let msg = this.std.msgs.get(key);
    if (!msg) {
      const found = this.dbcFind(f.id, false);
      msg = new Msg(this.std, key, found ? found.def : { name: `ID ${key}`, signals: [] }, found ? found.src : 'raw');
      this.std.msgs.set(key, msg);
      this.rev++;
      for (const v of msg.vars) this.added(v);
    }
    this.std.frames++;
    msg.update({ t: f.t, data: f.data, da: null, via: '', id: f.id });
  }

  /** A complete message (single frame or reassembled). */
  message(m) {
    const dev = this.device(m.sa, m.t);
    const key = hex(m.pgn, m.pgn > 0xffff ? 5 : 4);
    let msg = dev.msgs.get(key);
    if (!msg) {
      const found = this.dbcFind(m.id, true, m.pgn);
      msg = found ? new Msg(dev, key, found.def, found.src) : new Msg(dev, key, messageDef(m.pgn), 'iso');
      msg.pgn = m.pgn;
      dev.msgs.set(key, msg);
      this.rev++;
      for (const v of msg.vars) this.added(v);
    }
    msg.update(m);
    switch (m.pgn) {
      case PGN.ADDRESS_CLAIMED: if (m.data.length >= 8) this.claim(m.sa, nameFromBytes(m.data), m.t); break;
      case PGN.SOFTWARE_ID: this.softwareId(dev, m); break;
      case PGN.ECU_ID: this.ecuId(dev, m); break;
      case PGN.DM1: this.dm1(dev, m); break;
      default: break;
    }
    for (const p of this.plugins) if (p.pgns.has(m.pgn)) p.message(this, m, dev);
  }

  /** Address Claimed: bind the NAME to the address; a device that changes address keeps its tree. */
  claim(sa, name, t) {
    if (sa === 0xfe) return; // cannot claim
    const here = this.devices.get(sa);
    if (here?.nameBig === name) return;
    const known = this.all.find((d) => d.nameBig === name && d !== here);
    if (known) { // the same control function on a new address: it keeps its node
      if (this.devices.get(known.sa) === known) this.devices.delete(known.sa);
      known.sa = sa;
      known.lost = false;
      known.ident.v('Adres').setText(hex(sa), t);
      if (here && here.nameBig === null) { // frames from this address before the claim belong to it
        for (const [k, msg] of here.msgs) if (!known.msgs.has(k)) { msg.parent = known; known.msgs.set(k, msg); }
        known.frames += here.frames;
        this.all = this.all.filter((d) => d !== here);
      } else if (here) here.lost = true;
      this.devices.set(sa, known);
      this.rev++;
      return;
    }
    if (here && here.nameBig !== null) { // another control function took this address
      here.lost = true;
      const d = new Device(this, sa);
      d.firstT = t;
      this.devices.set(sa, d);
      this.all.push(d);
      d.setName(name, t);
      return;
    }
    this.device(sa, t).setName(name, t);
  }

  softwareId(dev, m) {
    const n = m.data[0];
    const text = String.fromCharCode(...m.data.subarray(1)).replace(/[\0\xff]+$/, '');
    const fields = text.replace(/\*$/, '').split('*').slice(0, n || undefined);
    dev.ident.v('Software').setText(fields.join(' | '), m.t);
    this.rev++;
  }

  ecuId(dev, m) {
    const text = String.fromCharCode(...m.data).replace(/[\0\xff]+$/, '');
    const f = text.split('*');
    const names = ['Onderdeelnummer', 'Serienummer', 'Locatie', 'Type', 'Fabrikant ECU', 'Hardware-ID'];
    names.forEach((nm, i) => { if (f[i] !== undefined && f[i] !== '') dev.ident.v(nm).setText(f[i], m.t); });
  }

  dm1(dev, m) {
    const { lamps, codes } = decodeDm(m.data);
    const D = dev.diag;
    const lampNames = { mil: 'Lamp MIL', red: 'Lamp rood (stop)', amber: 'Lamp oranje (waarschuwing)', protect: 'Lamp bescherming' };
    for (const [k, nm] of Object.entries(lampNames)) D.v(k, nm).setText(lamps[k], m.t);
    D.v('count', 'Actieve storingen').setNum(codes.length, m.t);
    const keep = new Set(['mil', 'red', 'amber', 'protect', 'count']);
    for (const c of codes) {
      const key = `SPN${c.spn}.${c.fmi}`;
      keep.add(key);
      const prop = c.spn >= 520192 && c.spn <= 524287 ? ' (fabrikantspecifiek)' : '';
      D.v(key, `SPN ${c.spn} FMI ${c.fmi}`).set({ value: c.oc, text: `×${c.oc} · ${FMI[c.fmi] ?? 'FMI ' + c.fmi}${prop}`, status: 'ok' }, m.t);
    }
    for (const k of [...D.items.keys()]) if (!keep.has(k)) { const v = D.items.get(k); this.setWatch(v, false); D.items.delete(k); this.rev++; }
  }

  /** Devices sorted by address (the 11-bit pseudo-device last). */
  deviceList() {
    const list = [...this.devices.values()].sort((a, b) => a.sa - b.sa);
    for (const d of this.all) if (!list.includes(d) && d !== this.std) list.push(d);
    if (this.std) list.push(this.std);
    return list;
  }

  /** Every variable (for search). */
  *variables() {
    yield* this.bus.items.values();
    for (const d of this.deviceList()) {
      for (const g of d.kids()) yield* walk(g);
    }
  }

  find(id) {
    for (const n of this.walkAll()) if (n.id === id) return n;
    return null;
  }

  *walkAll() {
    yield this.bus;
    yield* this.bus.items.values();
    for (const d of this.deviceList()) { yield d; for (const g of d.kids()) yield* walkNodes(g); }
  }
}

function* walk(node) {
  if (node.kind === 'var') { yield node; return; }
  for (const k of node.kids()) yield* walk(k);
}

function* walkNodes(node) {
  yield node;
  for (const k of node.kids()) yield* walkNodes(k);
}

/** The root: bus statistics as variables, devices as children. */
class BusNode {
  constructor(model) {
    this.model = model;
    this.kind = 'bus';
    this.items = new Map();
    this.ratesVar = this.v('Frames per seconde', { unit: 'fr/s', scale: 1 });
    this.loadVar = this.v('Busbelasting', { unit: '%', scale: 0.1 });
  }

  get id() { return 'bus'; }
  get label() { return 'Bus'; }
  get path() { return 'Bus'; }
  get stableKey() { return 'Bus'; }
  get device() { return null; }

  v(name, def) { const x = new Var(this, name, name, def); this.items.set(name, x); return x; }
  kids() { return [...this.items.values(), ...this.model.deviceList()]; }
}
