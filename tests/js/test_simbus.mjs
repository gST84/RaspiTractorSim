// Sending through the adapter (the online simulator on a real bus, web/can/simbus.js): the transmit encoders of
// the drivers (byte layouts of the Linux drivers) and the coupling between adapter and simulator.
import { section, test, testAsync, eq, ok } from './harness.mjs';
import { mcbaEncode, mcbaParse, gsEncode, gsParse, pcanEncode, pcanFdEncode } from '../../web/can/sources/usb.js';
import { slcanEncode, slcanLine } from '../../web/can/sources/slcan.js';
import { SimBus, loadBusConfig, saveBusConfig, BUS_DEFAULTS } from '../../web/can/simbus.js';

const hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
const u8 = (...a) => Uint8Array.from(a);
const tick = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
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
