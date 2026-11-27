import { section, test, eq, near, ok } from './harness.mjs';
import { Model } from '../../web/can/model.js';
import { frame, makeId } from '../../web/can/frames.js';
import { TcPlugin } from '../../web/can/tc.js';
import { encodeDdop, parseDdop } from '../../web/can/ddop.js';
import { ddiInfo, ddiText } from '../../web/can/ddi.js';

const pad = (s, n = 7) => Array.from(s.padEnd(n, ' ').slice(0, n), (c) => c.charCodeAt(0));
const POOL = [
  { table: 'DVC', id: 0, designator: 'Spuit', softwareVersion: '1.0', name: [1, 2, 3, 4, 5, 6, 7, 8], serial: '42', structureLabel: pad('v1'), localization: [0x6e, 0x6c, 0, 0, 0, 0, 0xff] },
  { table: 'DET', id: 1, type: 1, designator: 'Spuit', number: 0, parent: 0, children: [10, 12] },
  { table: 'DET', id: 2, type: 4, designator: 'Sectie 1', number: 1, parent: 1, children: [11] },
  { table: 'DPD', id: 10, ddi: 141, props: 1, triggers: 8, designator: 'Werkstand', presentation: 0xffff },
  { table: 'DPD', id: 11, ddi: 2, props: 1, triggers: 1, designator: 'Dosering', presentation: 20 },
  { table: 'DPT', id: 12, ddi: 67, value: 6000, designator: 'Werkbreedte', presentation: 21 },
  { table: 'DVP', id: 20, offset: 0, scale: 0.0001, decimals: 1, unit: 'L/ha' },
  { table: 'DVP', id: 21, offset: 0, scale: 0.001, decimals: 2, unit: 'm' },
];

/** A long message as TP RTS/CTS frames from sa to da. */
function tp(t, sa, da, pgn, data) {
  const out = [];
  const n = Math.ceil(data.length / 7);
  out.push(frame(t, makeId(7, 0xec00, sa, da), [16, data.length & 0xff, data.length >> 8, n, 0xff, pgn & 0xff, (pgn >> 8) & 0xff, pgn >> 16], true));
  for (let i = 1; i <= n; i++) {
    const chunk = Array.from(data.slice((i - 1) * 7, i * 7));
    out.push(frame(t, makeId(7, 0xeb00, sa, da), [i, ...chunk, ...Array(7 - chunk.length).fill(0xff)], true));
  }
  return out;
}
const pd = (cmd, el, ddi, v) => [(cmd & 0xf) | ((el & 0xf) << 4), el >> 4, ddi & 0xff, ddi >> 8, v & 0xff, (v >> 8) & 0xff, (v >> 16) & 0xff, (v >> 24) & 0xff];

function setup() {
  const m = new Model();
  const tc = new TcPlugin();
  m.plugins.push(tc);
  return { m, tc };
}

section('TC-procesdata');
test('DDOP heen en terug (versie 3) en DVC van versie 4 met extended structure label', () => {
  const b = encodeDdop(POOL);
  eq(parseDdop(b).map((o) => o.table + o.id), POOL.map((o) => o.table + o.id));
  const v4 = encodeDdop([POOL[0]]);
  const ext = Uint8Array.from([...v4, 3, 0x41, 0x42, 0x43, ...encodeDdop([POOL[1]])]);
  const p = parseDdop(ext);
  eq([p.length, p[0].extendedLabel, p[1].table], [2, [0x41, 0x42, 0x43], 'DET']);
});
test('DDOP opgevangen op de bus → elementen, eigenschap en waarden met DVP', () => {
  const { m } = setup();
  const pool = encodeDdop(POOL);
  m.push(tp(0, 0x80, 0xf7, 0xcb00, Uint8Array.from([0x61, ...pool])));
  m.push([frame(0.5, makeId(3, 0xcb00, 0xf7, 0xff), [0xfe, 0xff, 0xff, 0xff, 0x01, 0x80, 0, 0xff], true)]); // TC status
  m.push([frame(1, makeId(3, 0xcb00, 0x80, 0xf7), pd(3, 1, 2, 2000000), true)]); // actual from the implement
  m.push([frame(1.1, makeId(3, 0xcb00, 0x80, 0xf7), pd(3, 0, 141, 1), true)]);
  m.push([frame(1.2, makeId(3, 0xcb00, 0xf7, 0x80), pd(3, 1, 1, 2500000), true)]); // setpoint from the TC
  const g = m.devices.get(0x80).groups.get('TC');
  ok(g, 'groep Procesdata');
  const E0 = g.items.get('E0'), E1 = g.items.get('E1');
  eq([E0.label, E1.label], ['Spuit (element 0)', 'Sectie 1 (element 1)']);
  eq([E0.items.get('67').value, E0.items.get('67').unit, E0.items.get('67').text], [6, 'm', '6,00']);
  eq(E0.items.get('141').text, 'aan');
  const rate = E1.items.get('2');
  near(rate.value, 200, 1e-9);
  eq([rate.name, rate.unit, rate.text], ['Dosering', 'L/ha', '200,0']);
  const sp = E1.items.get('1.sp');
  eq([sp.name, sp.unit], ['Setpoint Volume Per Area Application Rate · setpunt', 'mm³/m²']);
  near(sp.value, 25000, 1e-6);
  eq(m.devices.get(0xf7).groups.get('TCSTAT').items.get('task').text, 'ja');
});
test('zonder DDOP: genummerde elementen en DDI-namen uit de tabel', () => {
  const { m } = setup();
  m.push([frame(0, makeId(3, 0xcb00, 0x81, 0xf7), pd(3, 5, 7, 12345), true)]);
  const E5 = m.devices.get(0x81).groups.get('TC').items.get('E5');
  eq([E5.label, E5.items.get('7').name, E5.items.get('7').value, E5.items.get('7').unit], ['Element 5', 'Actual Mass Per Area Application Rate', 12345, 'mg/m²']);
});
test('DDOP uit een bestand na de waarden: namen en schaal volgen', () => {
  const { m, tc } = setup();
  m.push([frame(0, makeId(3, 0xcb00, 0x80, 0xf7), pd(3, 1, 2, 2000000), true)]);
  const dev = m.devices.get(0x80);
  ok(tc.setDdop(m, dev, encodeDdop(POOL), 'spuit.bin'), 'geladen');
  m.push([frame(1, makeId(3, 0xcb00, 0x80, 0xf7), pd(3, 1, 2, 1000000), true)]);
  const v = dev.groups.get('TC').items.get('E1').items.get('2');
  eq([v.name, v.unit], ['Dosering', 'L/ha']);
  near(v.value, 100, 1e-9);
});
test('DDI-tabel: bekende, eigen en onbekende DDI, condensed work state', () => {
  eq(ddiInfo(67).name, 'Actual Working Width');
  ok(ddiInfo(60000).proprietary, 'eigen DDI');
  eq(ddiInfo(9999).name, 'DDI 9999');
  eq(ddiText(ddiInfo(161), 0xfffffff4), '□■');
});
