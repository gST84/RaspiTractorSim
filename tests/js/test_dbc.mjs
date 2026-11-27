import { section, test, eq, near, ok } from './harness.mjs';
import { parseDbc } from '../../web/can/dbc.js';
import { Model } from '../../web/can/model.js';
import { frame } from '../../web/can/frames.js';

const DBC = `VERSION ""

BS_:

BU_: ECU1

BO_ 2364540158 EEC1: 8 Vector__XXX
 SG_ EngSpeed : 24|16@1+ (0.125,0) [0|8031.875] "rpm" Vector__XXX
 SG_ TorqueMode : 0|4@1+ (1,0) [0|15] "" Vector__XXX

BO_ 291 Status: 8 ECU1
 SG_ Mux M : 0|8@1+ (1,0) [0|255] "" ECU1
 SG_ TempA m1 : 15|16@0- (0.1,0) [-100|100] "°C" ECU1
 SG_ Mode m2 : 8|2@1+ (1,0) [0|3] "" ECU1
 SG_ Ratio : 32|32@1+ (1,0) [0|1] "" ECU1

CM_ BO_ 291 "Statusbericht van ECU1";
CM_ SG_ 291 TempA "Temperatuur A,
over twee regels";
VAL_ 291 Mode 0 "uit" 1 "aan" 2 "fout" 3 "n.b." ;
SIG_VALTYPE_ 291 Ratio : 1;
BA_DEF_ BO_ "VFrameFormat" ENUM "StandardCAN","ExtendedCAN","reserved","J1939PG";
BA_ "VFrameFormat" BO_ 2364540158 3;
`;

const f32 = (v) => { const b = new Uint8Array(4); new DataView(b.buffer).setFloat32(0, v, true); return Array.from(b); };
const db = parseDbc(DBC);

section('DBC');
test('berichten, J1939PG op PGN, commentaar over twee regels', () => {
  eq(db.messages.map((m) => [m.name, m.id.toString(16), m.ext, m.j1939]), [['EEC1', 'cf004fe', true, true], ['Status', '123', false, false]]);
  ok(db.byPgn.has(0xf004), 'PGN F004');
  eq(db.messages[1].comment, 'Statusbericht van ECU1');
  eq(db.messages[1].signals.find((s) => s.name === 'TempA').comment, 'Temperatuur A,\nover twee regels');
});
test('signaalvelden: Motorola signed, multiplex, keuzelijst, float', () => {
  const S = Object.fromEntries(db.messages[1].signals.map((s) => [s.name, s]));
  eq([S.TempA.order, S.TempA.signed, S.TempA.mux.value, S.TempA.mux.by], ['be', true, 1, 'Mux']);
  ok(S.Mux.isMux, 'Mux is de multiplexer');
  eq(S.Mode.values['1'], 'aan');
  eq(S.Ratio.type, 'float');
  eq(db.messages[0].signals[0].na, 'j1939');
});
test('model: J1939-bericht van een ander bronadres via de PGN', () => {
  const m = new Model();
  m.dbcs.push({ name: 'test.dbc', enabled: true, db });
  m.push([frame(0, 0x0cf004f0, [0xf0, 0, 0, 0xe0, 0x2e, 0, 0, 0], true)]);
  const msg = m.devices.get(0xf0).msgs.get('F004');
  eq([msg.src, msg.name], ['dbc:test.dbc', 'EEC1']);
  eq(msg.vars.find((v) => v.name === 'EngSpeed').value, 1500);
});
test('model: multiplex en float in een 11-bit bericht', () => {
  const m = new Model();
  m.dbcs.push({ name: 'test.dbc', enabled: true, db });
  m.push([frame(0, 0x123, [1, 0x01, 0x2c, 0, ...f32(0.5)], false)]);
  const V = Object.fromEntries(m.std.msgs.get('123').vars.map((v) => [v.name, v]));
  near(V.TempA.value, 30, 1e-9);
  eq(V.Mode.status, 'none');
  eq(V.Ratio.value, 0.5);
  m.push([frame(1, 0x123, [2, 0x01, 0, 0, ...f32(0.25)], false)]);
  eq(V.Mode.text, 'aan');
  near(V.TempA.value, 30, 1e-9); // keeps its last value while the other page is sent
});
test('model: DBC later inladen vervangt de ingebouwde definitie, volglijst blijft', () => {
  const m = new Model();
  m.push([frame(0, 0x0cf00400, [0xf0, 0, 0, 0xe0, 0x2e, 0, 0, 0], true)]);
  const msg = m.devices.get(0).msgs.get('F004');
  eq(msg.src, 'iso');
  m.setWatch(msg.vars.find((v) => v.name === 'Motortoerental'), true);
  m.dbcs.push({ name: 'test.dbc', enabled: true, db });
  m.refreshDefs();
  eq([msg.src, msg.vars.find((v) => v.name === 'EngSpeed').value], ['dbc:test.dbc', 1500]);
  m.dbcs[0].enabled = false;
  m.refreshDefs();
  eq(msg.src, 'iso');
  eq([...m.watch].map((v) => v.name), ['Motortoerental']);
});
