// The adapter protocols with made-up buffers, laid out as the Linux drivers describe them.
import { section, test, eq } from './harness.mjs';
import { mcbaParse, mcbaEncodeRx, gsParse, bitTiming, pcanParse, pcanFdParse, ucanPackets } from '../../web/can/sources/usb.js';
import { slcanLine } from '../../web/can/sources/slcan.js';

const D8 = [1, 2, 3, 4, 5, 6, 7, 8];
const le32 = (v) => [v & 0xff, (v >>> 8) & 0xff, (v >>> 16) & 0xff, (v >>> 24) & 0xff];
const le16 = (v) => [v & 0xff, (v >> 8) & 0xff];
const show = (f) => [f.id, f.ext, Array.from(f.data)];

section('USB- en seriële adapters');
test('Microchip: 29- en 11-bit frame, keep-alive met tellers', () => {
  const ka = new Uint8Array(19);
  ka.set([0xf5, 3, 4, 0, 0, 0, 250, 7, 0]);
  const buf = new Uint8Array([...mcbaEncodeRx(0x18fef100, true, D8), ...mcbaEncodeRx(0x123, false, [0xaa, 0xbb]), ...ka]);
  const out = [], st = {};
  mcbaParse(buf, 1, out, st);
  eq(out.map(show), [[0x18fef100, true, D8], [0x123, false, [0xaa, 0xbb]]]);
  eq([st.txErr, st.rxErr, st.kbps, st.rxLost], [3, 4, 250, 7]);
});
test('Microchip: ID-indeling zoals mcba_usb.c (sid/eid big-endian)', () => {
  // 0x0CFE48F0: SID = 0x0CFE48F0 >> 18 = 0x33F, EID = 0x248F0
  const m = mcbaEncodeRx(0x0cfe48f0, true, [0]);
  const sid = (m[3] << 8) | m[4];
  eq([(sid & 0xffe0) >> 5, sid & 0x08, sid & 3, (m[1] << 8) | m[2]], [0x33f, 8, 2, 0x48f0]);
});
test('gs_usb: ontvangen frame, TX-echo en foutframe', () => {
  const f = (echo, id, dlc, data, flags = 0) => new Uint8Array([...le32(echo), ...le32(id), dlc, 0, flags, 0, ...data, ...Array(8 - data.length).fill(0)]);
  const st = {};
  eq(show(gsParse(f(0xffffffff, 0x98fef100, 8, D8), 1, st)), [0x18fef100, true, D8]);
  eq(show(gsParse(f(0xffffffff, 0x123, 2, [9, 9]), 1, st)), [0x123, false, [9, 9]]);
  eq(gsParse(f(5, 0x98fef100, 8, D8), 1, st), null);
  eq(gsParse(f(0xffffffff, 0x20000004, 8, D8), 1, st), null);
  gsParse(f(0xffffffff, 0x123, 0, [], 1), 1, st);
  eq([st.errors, st.lost], [1, 1]);
});
test('bit timing: candleLight 48 MHz en PCAN-USB FD 80 MHz op 250 kbit/s (87,5 %)', () => {
  eq(bitTiming(48e6, 250000), { brp: 12, tq: 16, tseg1: 13, tseg2: 2, sjw: 1 });
  eq(bitTiming(80e6, 250000, { tseg1Max: 256, tseg2Max: 128 }), { brp: 20, tq: 16, tseg1: 13, tseg2: 2, sjw: 1 });
  eq(bitTiming(80e6, 500000, { tseg1Max: 256, tseg2Max: 128 }).brp, 10);
});
test('PCAN-USB: records met 16- en 8-bit tijdstempel, plus een statusrecord', () => {
  const buf = new Uint8Array([
    0x00, 3,
    0x20 | 8, ...le32(0x18fef100 << 3 >>> 0), 0x34, 0x12, ...D8, // ext, first record: 2-byte time stamp
    0x40 | 0x80 | 0, 3, 0, 0x40, // internal: bus load (f 3), with a 1-byte time stamp, 1 payload byte
    0x77,
    2, ...le16(0x123 << 5), 0x41, 0xaa, 0xbb, // std, 1-byte time stamp
  ]);
  const out = [], st = {};
  pcanParse(buf, 1, out, st);
  eq(out.map(show), [[0x18fef100, true, D8], [0x123, false, [0xaa, 0xbb]]]);
});
test('PCAN-USB FD: uCAN-record met 29-bit frame, overrun', () => {
  const rec = [...le16(36), ...le16(1), ...Array(16).fill(0), 8 << 4, 0, ...le16(0x02), ...le32(0x18fef100), ...D8];
  const ovr = [...le16(16), ...le16(0x101), ...Array(12).fill(0)];
  const out = [], st = {};
  pcanFdParse(new Uint8Array([...rec, ...ovr, 0, 0, 0, 0]), 1, out, st);
  eq(out.map(show), [[0x18fef100, true, D8]]);
  eq(st.lost, 1);
});
test('PCAN-USB FD: opdrachtpakketten van ≤ 64 byte met "end of collection"', () => {
  const rows = Array.from({ length: 64 }, (_, i) => [8, 0, i, 0, 255, 255, 255, 255]);
  const p = ucanPackets(rows);
  eq([p.length, p[0].length, p.at(-1).length], [10, 64, 16]);
  eq(Array.from(p[0].slice(56)), Array(8).fill(0xff));
  eq(Array.from(p[0].slice(0, 8)), [8, 0, 0, 0, 255, 255, 255, 255]);
});
test('slcan: T- en t-regels, remote frame genegeerd', () => {
  eq(show(slcanLine('T18FEF10080102030405060708', 1)), [0x18fef100, true, D8]);
  eq(show(slcanLine('t1232AABB', 1)), [0x123, false, [0xaa, 0xbb]]);
  eq(slcanLine('r1230', 1), null);
  eq(slcanLine('T18FE', 1), null);
});
