// Sending through the adapter (the online simulator on a real bus, web/can/simbus.js): the transmit encoders of
// the drivers (byte layouts of the Linux drivers) and the coupling between adapter and simulator.
import { section, test, testAsync, eq, ok } from './harness.mjs';
import { UsbSource, mcbaEncode, mcbaEncodeRx, mcbaParse, gsEncode, gsParse, pcanEncode, pcanFdEncode } from '../../web/can/sources/usb.js';
import { slcanEncode, slcanLine } from '../../web/can/sources/slcan.js';
import { SimBus, loadBusConfig, saveBusConfig, BUS_DEFAULTS } from '../../web/can/simbus.js';

const hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
const u8 = (...a) => Uint8Array.from(a);
const tick = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
SimBus.OPEN_MS = 300; // short: jsc lets a cancelled timer run out before it exits
const memStore = () => ({ m: {}, getItem(k) { return this.m[k] ?? null; }, setItem(k, v) { this.m[k] = v; } });

section('Zenden via de adapter');
test('Microchip: 0xA3 met SID/EID zoals mcba_usb.c (en tractorsim/canbus.py)', () => {
  eq(hex(mcbaEncode(0x18fef100, true, u8(1, 2))), 'a3f100c7ea020102' + '00'.repeat(11));
  const out = [];
  mcbaParse(mcbaEncode(0x123, false, u8(0xaa), 0xe3), 0, out, {});
  eq([out[0].id, out[0].ext, out[0].data], [0x123, false, [0xaa]]);
});
test('gs_usb: host frame met echo_id en de 29-bitvlag', () => {
  const b = gsEncode(0x18fef100, true, u8(1, 2, 3), 7);
  eq(hex(b), '07000000' + '00f1fe98' + '03000000' + '0102030000000000');
  b.set([0xff, 0xff, 0xff, 0xff], 0); // the same layout as a received frame
  const f = gsParse(b, 0, {});
  eq([f.id, f.ext, f.data], [0x18fef100, true, [1, 2, 3]]);
});
test('PCAN-USB: buffer van 64 bytes, id << 3 (29 bit) of << 5 (11 bit), teller in de laatste byte', () => {
  const b = pcanEncode(0x18fef100, true, u8(1, 2), 5);
  eq([b.length, hex(b.slice(0, 9)), b[63]], [64, '0201220088f7c70102', 5]);
  eq(hex(pcanEncode(0x123, false, u8(0xaa)).slice(0, 6)), '0201016024aa');
});
test('PCAN-USB FD: pucan_tx_msg (32-bit uitgelijnd) en een slotrecord van 0 bytes', () => {
  eq(hex(pcanFdEncode(0x18fef100, true, u8(1, 2, 3))),
    '1800' + '0010' + '00000000' + '00000000' + '30' + '00' + '0200' + '00f1fe18' + '01020300' + '00000000');
  eq(pcanFdEncode(0x7ff, false, new Uint8Array(8)).length, 28 + 4);
});
test('slcan: T/t-commando, terug te lezen als frame', () => {
  eq(slcanEncode(0x18fef100, true, u8(1, 2)), 'T18FEF10020102\r');
  eq(slcanEncode(0x123, false, u8(0xaa)), 't1231AA\r');
  const f = slcanLine('T18FEF10020102', 0);
  eq([f.id, f.ext, f.data], [0x18fef100, true, [1, 2]]);
});
test('instellingen: standaard demo, bewaard per browser, kapotte waarde → standaard', () => {
  const store = memStore();
  eq(loadBusConfig(store), BUS_DEFAULTS);
  saveBusConfig({ ...BUS_DEFAULTS, demo: false, kind: 'serial', baud: 921600 }, store);
  eq([new SimBus(store).cfg.demo, new SimBus(store).cfg.kind, new SimBus(store).cfg.baud], [false, 'serial', 921600]);
  store.m['tractorsim.bus'] = '{kapot';
  eq(loadBusConfig(store).demo, true);
});

class FakeSrc {
  constructor() { this.label = 'Nep-adapter · 250 kbit/s'; this.batch = 2; this.sent = []; this.batches = []; this.hold = null; }
  async open(sink) { this.sink = sink; sink.status('open'); }
  async send(fs) { if (this.hold) await this.hold; this.batches.push(fs.length); this.sent.push(...fs); }
  async close() { this.closedByUs = true; }
}

await testAsync('koppeling: ontvangen naar de simulator, zenden in volgorde en in groepjes, vol, losgekoppeld', async () => {
  const bus = new SimBus(memStore());
  const got = [], states = [];
  bus.attach({ receive: (t) => got.push(...JSON.parse(t)), state: (up, label) => states.push([up, label]) });
  ok(bus.active, 'actief');
  eq(bus.send(0x18fef100, 1, '0102'), 0, 'zonder adapter');
  const src = new FakeSrc();
  await bus.start(src);
  eq(states, [[true, 'Nep-adapter · 250 kbit/s']]);
  src.sink.frames([{ id: 0x123, ext: false, data: u8(0xaa) }, { id: 0x18eafffe, ext: true, data: u8(0, 0xee, 0) }]);
  eq(got, [[0x123, 0, 'aa'], [0x18eafffe, 1, '00ee00']]);
  eq([bus.send(0x100, 1, '00'), bus.send(0x101, 1, '01'), bus.send(0x102, 1, '0203')], [1, 1, 1]);
  await tick();
  eq([src.sent.map((f) => f.id), src.batches, src.sent[2].data], [[0x100, 0x101, 0x102], [1, 2], [2, 3]]);
  let release;
  src.hold = new Promise((r) => { release = r; });
  const codes = [];
  for (let i = 0; i < 520; i++) codes.push(bus.send(0x200, 1, ''));
  eq([codes.filter((c) => c === 1).length, codes.at(-1)], [513, 2], 'wachtrij vol'); // one in flight + 512 waiting
  release();
  await tick();
  src.sink.closed('USB: losgekoppeld');
  eq([states.at(-1), bus.st.connected, bus.st.error, bus.send(0x100, 1, '')], [[false, ''], false, 'USB: losgekoppeld', 0]);
});

/** A Microchip CAN BUS Analyzer behind WebUSB. Like its firmware, it only takes a command while the host is reading
 *  (an IN transfer is waiting); `dead`: it takes nothing at all. */
class FakeAnalyzer {
  constructor() {
    Object.assign(this, { vendorId: 0x04d8, productId: 0x0a30, configuration: null, reads: [], outs: [], dead: false });
  }
  async open() {}
  async selectConfiguration() {
    this.configuration = { interfaces: [{ interfaceNumber: 0, alternate: { endpoints: [
      { type: 'bulk', direction: 'in', endpointNumber: 1 }, { type: 'bulk', direction: 'out', endpointNumber: 1 }] } }] };
  }
  async claimInterface() {}
  transferIn() { return new Promise((resolve, reject) => this.reads.push({ resolve, reject })); }
  transferOut(ep, data) {
    if (this.dead || !this.reads.length) return new Promise(() => {});
    this.outs.push([ep, ...data.slice(0, 3)]);
    return Promise.resolve({ status: 'ok', bytesWritten: data.length });
  }
  deliver(bytes) { this.reads.shift().resolve({ status: 'ok', data: new DataView(bytes.buffer) }); }
  async close() { for (const r of this.reads.splice(0)) r.reject(new Error('closed')); }
}
const quietSink = (log = []) => ({ status: (t) => log.push(t), frames() {}, lost() {}, closed() {} });

await testAsync('Microchip via WebUSB: eerst lezen, dan bitrate en firmware vragen; daarna zenden en ontvangen', async () => {
  const dev = new FakeAnalyzer();
  const src = new UsbSource(dev, 250000, { listen: false });
  const texts = [], frames = [];
  await src.open({ ...quietSink(texts), frames: (fs) => frames.push(...fs) });
  eq(dev.outs, [[1, 0xa1, 0x00, 0xfa], [1, 0xa9, 1, 0], [1, 0xa9, 2, 0]]);
  eq(texts.at(-1), 'Microchip CAN BUS Analyzer · 250 kbit/s');
  ok(texts.some((t) => t.includes('USB openen')) && texts.some((t) => t.includes('instellen')), 'stappen in de status');
  await src.send([{ id: 0x18fef100, ext: true, data: u8(1, 2) }]);
  eq(dev.outs.at(-1), [1, 0xa3, 0xf1, 0x00]);
  dev.deliver(mcbaEncodeRx(0x18eafffe, true, u8(0, 0xee, 0)));
  await tick();
  eq([frames.length, frames[0]?.id], [1, 0x18eafffe]);
  await src.close();
});

await testAsync('analyzer die niets aanneemt: time-out met uitleg, niet eeuwig "verbinden…"', async () => {
  const dev = new FakeAnalyzer();
  dev.dead = true;
  const before = UsbSource.TIMEOUT_MS;
  UsbSource.TIMEOUT_MS = 30;
  try {
    const src = new UsbSource(dev, 250000, { listen: false });
    let err = null;
    try { await src.open(quietSink()); } catch (e) { err = e; }
    ok(err && /time-out/.test(err.message), `fout: ${err && err.message}`);
    ok(src.closed && dev.reads.length === 0, 'adapter weer gesloten');
  } finally {
    UsbSource.TIMEOUT_MS = before;
  }
});

await testAsync('Verbreken tijdens het verbinden breekt de poging af', async () => {
  const bus = new SimBus(memStore());
  const states = [];
  bus.attach({ receive() {}, state: (up) => states.push(up) });
  const src = { label: 'hangt', closes: 0, open() { return new Promise((_, reject) => { this.reject = reject; }); },
    async close() { this.closes++; this.reject?.(new Error('gesloten')); } };
  const attempt = bus.start(src).catch((e) => e);
  await tick();
  await bus.disconnect();
  const e = await attempt;
  ok(e instanceof Error, 'poging mislukt');
  eq([bus.st.connected, states, src.closes >= 1, bus.send(1, 1, '')], [false, [], true, 0]);
});
