import { section, test, eq, near, ok } from './harness.mjs';
import { splitId, makeId, frame } from '../../web/can/frames.js';
import { rawBits, decodeSignal, position } from '../../web/can/signals.js';
import { MESSAGES, nameDecode, nameFromBytes, decodeDm } from '../../web/can/isobus-db.js';
import { Model } from '../../web/can/model.js';
import { parseLog, detect, toCandump } from '../../web/can/sources/logfile.js';

const sig = (pgn, name) => MESSAGES.get(pgn).signals.find((s) => s.name === name);
const dec = (pgn, name, data) => decodeSignal(Uint8Array.from(data), sig(pgn, name));
const le16 = (w) => [w & 0xff, (w >> 8) & 0xff];
const le32 = (d) => [d & 0xff, (d >>> 8) & 0xff, (d >>> 16) & 0xff, (d >>> 24) & 0xff];

function nameBytes({ identity = 1234, manufacturer = 0, ecuInstance = 0, functionInstance = 0, fn = 134, deviceClass = 0, dci = 0, ig = 2, self = 1 }) {
  const n = BigInt(identity) | (BigInt(manufacturer) << 21n) | (BigInt(ecuInstance) << 32n) | (BigInt(functionInstance) << 35n)
    | (BigInt(fn) << 40n) | (BigInt(deviceClass) << 49n) | (BigInt(dci) << 56n) | (BigInt(ig) << 60n) | (BigInt(self) << 63n);
  const b = [];
  for (let i = 0; i < 8; i++) b.push(Number((n >> BigInt(8 * i)) & 0xffn));
  return b;
}

const F = (t, prio, pgn, sa, da, data) => frame(t, makeId(prio, pgn, sa, da), data, true);
const v = (model, sa, pgnKey, name) => model.devices.get(sa).msgs.get(pgnKey).vars.find((x) => x.name === name);

section('frames en signalen');
test('ID splitsen: PDU2 en PDU1', () => {
  eq(splitId(0x0cfe48f0), { prio: 3, pgn: 0xfe48, sa: 0xf0, da: 0xff });
  eq(splitId(0x18e72680), { prio: 6, pgn: 0xe700, sa: 0x80, da: 0x26 });
  eq(makeId(6, 0xe700, 0x80, 0x26), 0x18e72680);
});
test('Intel: byte-uitgelijnd, losse bits en 64 bit', () => {
  eq(rawBits(Uint8Array.from([0x34, 0x12]), 0, 16), 0x1234);
  eq(rawBits(Uint8Array.from([0b10110100]), 2, 3), 0b101);
  eq(rawBits(Uint8Array.from([1, 0, 0, 0, 0, 0, 0, 0x80]), 0, 64), 0x8000000000000001n);
});
test('Motorola (DBC): startbit 7 / 16 bit en startbit 3 / 12 bit', () => {
  eq(rawBits(Uint8Array.from([0x12, 0x34]), 7, 16, 'be'), 0x1234);
  eq(rawBits(Uint8Array.from([0xab, 0xcd]), 3, 12, 'be'), 0xbcd);
});
test('signed en schaal', () => {
  const r = decodeSignal(Uint8Array.from([0xfe, 0xff]), { name: 'x', start: 0, len: 16, order: 'le', signed: true, scale: 0.5, offset: 0 });
  eq(r.value, -1);
});
test('J1939: n.b. en fout voor 8 en 16 bit, 2-bit status', () => {
  eq(dec(0xfe45, 'Positie', [0xff, 0, 0, 0, 0, 0, 0, 0]).status, 'na');
  eq(dec(0xfe45, 'Positie', [0xfe, 0, 0, 0, 0, 0, 0, 0]).status, 'err');
  eq(dec(0xfe48, 'Snelheid', [0xff, 0xff, 0, 0, 0, 0, 0, 0]).status, 'na');
  eq(dec(0xfe48, 'Snelheid', [0x00, 0xfe, 0, 0, 0, 0, 0, 0]).status, 'err');
  eq(dec(0xfe48, 'Rijrichting', [0, 0, 0, 0, 0, 0, 0, 3]).text, 'n.b.');
});
test('bericht te kort: status "short"', () => eq(dec(0xfe48, 'Afstand', [1, 2, 3]).status, 'short'));
test('positie in ISO-notatie', () => {
  eq(position(sig(0xfe48, 'Snelheid')), 'byte 1-2');
  eq(position(sig(0xfe48, 'Sleutelschakelaar')), 'byte 8 bit 4-3');
});

section('ingebouwde berichten (zelfde regels als tecu.py en ISO_TractorDecode)');
test('Wheel-based Speed: snelheid, afstand, richting, sleutel', () => {
  const d = [...le16(3333), ...le32(1234500), 10, 1 | (1 << 2) | (3 << 4)];
  near(dec(0xfe48, 'Snelheid', d).value, 3.333, 1e-9);
  near(dec(0xfe48, 'Snelheid (km/h)', d).value, 11.9988, 1e-9);
  near(dec(0xfe48, 'Afstand', d).value, 1234.5, 1e-9);
  eq(dec(0xfe48, 'Rijrichting', d).text, 'vooruit');
  eq(dec(0xfe48, 'Sleutelschakelaar', d).text, 'aan');
  eq(dec(0xfe48, 'Start/stop', d).status, 'na');
  eq(dec(0xfe48, 'Max. tijd tractorvoeding', d).value, 10);
});
test('EEC1: toerental 1500 rpm, koppel 30 %', () => {
  const d = [0xf0, 125, 155, ...le16(12000), 0xff, 0xf0, 125];
  eq(dec(0xf004, 'Motortoerental', d).value, 1500);
  eq(dec(0xf004, 'Actueel koppel', d).value, 30);
  eq(dec(0xf004, 'Koppelmodus', d).text, 'lage stationair');
  eq(dec(0xf004, 'Bronadres regeling', d).status, 'na');
});
test('Rear Hitch: positie 40 %, in werkstand, trekkracht 1000 N', () => {
  const d = [100, 0x07 | (1 << 6), 125, ...le16(32100), 0xc0, 0xff, 0xff];
  eq(dec(0xfe45, 'Positie', d).value, 40);
  eq(dec(0xfe45, 'In werkstand', d).text, 'in werkstand');
  eq(dec(0xfe45, 'Trekkracht', d).value, 1000);
  eq(dec(0xfe45, 'Nominale onderstangkracht', d).value, 0);
});
test('Rear PTO: 540 rpm, ingeschakeld, 1000-modus uit', () => {
  const d = [...le16(4320), ...le16(4320), 3 | (0 << 2) | (0 << 4) | (1 << 6), 0x01 | (3 << 4) | (3 << 6), 0xc0, 0xff];
  eq(dec(0xfe43, 'Toerental', d).value, 540);
  eq(dec(0xfe43, 'Ingeschakeld', d).text, 'aan');
  eq(dec(0xfe43, '1000-modus', d).text, 'uit');
});
test('Hitch and PTO Commands (ISO_TractorEcu-test): achterhef 60 %, voorhef 40 %, aftakas', () => {
  const d = [100, 150, ...le16(4320), ...le16(8000), (1 << 6) | (0 << 4) | 0x0f, (0 << 6) | (1 << 4) | (3 << 2) | 1];
  eq(dec(0xfe42, 'Achterhef positie', d).value, 60);
  eq(dec(0xfe42, 'Fronthef positie', d).value, 40);
  eq(dec(0xfe42, 'Aftakas achter toerental', d).value, 1000);
  eq(dec(0xfe42, 'Aftakas voor toerental', d).value, 540);
  eq(dec(0xfe42, 'Aftakas voor inschakelen', d).text, 'aan');
  eq(dec(0xfe42, 'Aftakas achter 1000-modus', d).text, 'aan');
  eq(dec(0xfe42, 'Aftakas voor economy', d).text, 'n.b.');
});
test('Tractor Facilities: klasse 3', () => eq(dec(0xfe09, 'Klasse', [(2 << 6) | 0x3f, 0, 0, 0, 0, 0, 0, 0]).text, 'klasse 3'));
test('Language Command: nl, komma, metrisch, BE', () => {
  const d = [110, 108, 0x0f, 0, 0, 0, 66, 69];
  eq(dec(0xfe0f, 'Taal', d).text, 'nl');
  eq(dec(0xfe0f, 'Decimaalteken', d).text, 'komma');
  eq(dec(0xfe0f, 'Land', d).text, 'BE');
});
test('Vehicle Position: 1e-7 °, offset -210', () => {
  const lat = 50.98765432;
  const raw = Math.round((lat + 210) / 1e-7);
  near(dec(0xfef3, 'Breedte', [...le32(raw), 0, 0, 0, 0]).value, lat, 1e-7);
});
test('NAME: velden en tekst', () => {
  const n = nameFromBytes(nameBytes({ fn: 134, ig: 2, identity: 77 }));
  const f = nameDecode(n);
  eq([f.function, f.industryGroup, f.identity, f.selfConfigurable], [134, 2, 77, 1]);
});
test('DM1: SPN 520192 FMI 31 ×3, oranje lamp', () => {
  const r = decodeDm(Uint8Array.from([0x04, 0xff, 0x00, 0xf0, 0xff, 0x03, 0xff, 0xff]));
  eq(r.lamps.amber, 'aan');
  eq(r.codes, [{ spn: 520192, fmi: 31, oc: 3, cm: 0 }]);
});

section('model en transport');
test('Address Claimed → toestel "F0 · Tractor ECU" met NAME-velden', () => {
  const m = new Model();
  m.push([F(0, 6, 0xee00, 0xf0, 0xff, nameBytes({ fn: 134 }))]);
  const d = m.devices.get(0xf0);
  eq(d.label, 'F0 · Tractor ECU');
  eq(d.ident.items.get('Functie').text, 'Tractor ECU (134)');
});
test('waarden, periode en min/max van een variabele', () => {
  const m = new Model();
  for (let i = 0; i < 11; i++) m.push([F(i * 0.1, 3, 0xfe48, 0xf0, 0xff, [...le16(1000 + i * 100), ...le32(0), 0xff, 0xfd])]);
  const x = v(m, 0xf0, 'FE48', 'Snelheid');
  near(x.value, 2.0, 1e-9);
  near(x.min, 1.0, 1e-9);
  near(m.devices.get(0xf0).msgs.get('FE48').period, 0.1, 1e-6);
  eq(x.path, 'F0/FE48/Snelheid');
});
test('TP BAM: Software Identification van 0x80', () => {
  const m = new Model();
  const txt = [2, ...'v1.2*abc*'].map((c) => (typeof c === 'string' ? c.charCodeAt(0) : c));
  m.push([
    F(0, 7, 0xec00, 0x80, 0xff, [32, txt.length, 0, 2, 0xff, 0xda, 0xfe, 0x00]),
    F(0.05, 7, 0xeb00, 0x80, 0xff, [1, ...txt.slice(0, 7)]),
    F(0.10, 7, 0xeb00, 0x80, 0xff, [2, ...txt.slice(7), ...Array(14 - txt.length).fill(0xff)]),
  ]);
  eq(m.devices.get(0x80).ident.items.get('Software').text, 'v1.2 | abc');
});
test('TP RTS/CTS: 20 bytes ECU → VT', () => {
  const m = new Model();
  const data = Array.from({ length: 20 }, (_, i) => (i === 0 ? 0x11 : i));
  const fr = [F(0, 7, 0xec00, 0x80, 0x26, [16, 20, 0, 3, 0xff, 0x00, 0xe7, 0x00]), F(0.01, 7, 0xec00, 0x26, 0x80, [17, 3, 1, 0xff, 0xff, 0x00, 0xe7, 0x00])];
  for (let n = 1; n <= 3; n++) fr.push(F(0.01 + n * 0.01, 7, 0xeb00, 0x80, 0x26, [n, ...[...data, 0xff].slice((n - 1) * 7, n * 7)]));
  m.push(fr);
  const msg = m.devices.get(0x80).msgs.get('E700');
  eq(msg.data.length, 20);
  eq(msg.vars[0].text, 'Object Pool Transfer');
  eq(msg.via, 'TP');
});
test('ETP: 1800 bytes met twee DPO', () => {
  const m = new Model();
  const data = Array.from({ length: 1800 }, (_, i) => i & 0xff);
  const fr = [F(0, 7, 0xc800, 0x80, 0x26, [20, ...le32(1800), 0x00, 0xe7, 0x00])];
  fr.push(F(0, 7, 0xc800, 0x80, 0x26, [22, 255, 0, 0, 0, 0x00, 0xe7, 0x00]));
  for (let s = 1; s <= 255; s++) fr.push(F(0, 7, 0xc700, 0x80, 0x26, [s, ...data.slice((s - 1) * 7, s * 7)]));
  fr.push(F(0, 7, 0xc800, 0x80, 0x26, [22, 3, 255, 0, 0, 0x00, 0xe7, 0x00]));
  for (let s = 1; s <= 3; s++) {
    const chunk = data.slice((254 + s) * 7, (255 + s) * 7);
    fr.push(F(0, 7, 0xc700, 0x80, 0x26, [s, ...chunk, ...Array(7 - chunk.length).fill(0xff)]));
  }
  m.push(fr);
  const msg = m.devices.get(0x80).msgs.get('E700');
  eq(msg.data.length, 1800);
  eq(Array.from(msg.data.slice(1790)), data.slice(1790));
  eq(msg.via, 'ETP');
});
test('fast packet 129029: 43 bytes in 7 frames', () => {
  const m = new Model();
  const g = new Uint8Array(43);
  const dv = new DataView(g.buffer);
  g[0] = 9;
  dv.setUint16(1, 20418, true);
  dv.setUint32(3, (14 * 3600 + 5 * 60 + 37) * 10000, true);
  dv.setBigInt64(7, BigInt(Math.round(50.98765432 * 1e8)) * 100000000n, true);
  dv.setBigInt64(15, BigInt(Math.round(3.7654321 * 1e8)) * 100000000n, true);
  dv.setBigInt64(23, 17250000n, true);
  g[31] = 4 << 4; g[32] = 0xfc; g[33] = 14;
  dv.setInt16(34, 80, true); dv.setInt16(36, 150, true); dv.setInt32(38, 4710, true);
  const fr = [];
  for (let n = 0; n < 7; n++) {
    const f = Array(8).fill(0xff);
    f[0] = (3 << 5) | n;
    if (n === 0) { f[1] = 43; for (let i = 0; i < 6; i++) f[2 + i] = g[i]; }
    else for (let i = 0; i < 7; i++) { const k = 6 + 7 * (n - 1) + i; if (k < 43) f[1 + i] = g[k]; }
    fr.push(frame(n * 0.001, 0x0df805f0, f, true));
  }
  m.push(fr);
  const msg = m.devices.get(0xf0).msgs.get('1F805');
  near(msg.vars.find((x) => x.name === 'Breedte').value, 50.98765432, 1e-8);
  eq(msg.vars.find((x) => x.name === 'Methode').text, 'RTK fixed');
  near(msg.vars.find((x) => x.name === 'HDOP').value, 0.8, 1e-9);
  near(msg.vars.find((x) => x.name === 'Hoogte').value, 17.25, 1e-9);
});
test('adreswissel: zelfde NAME op een nieuw adres houdt zijn knooppunt', () => {
  const m = new Model();
  const nb = nameBytes({ fn: 130, identity: 5 });
  m.push([F(0, 6, 0xee00, 0x80, 0xff, nb), F(0.1, 3, 0xfe48, 0x80, 0xff, [0, 0, 0, 0, 0, 0, 0xff, 0xff])]);
  const d = m.devices.get(0x80);
  m.push([F(1, 6, 0xee00, 0x81, 0xff, nb)]);
  ok(m.devices.get(0x81) === d, 'zelfde toestel op 0x81');
  ok(!m.devices.has(0x80), '0x80 vrij');
  ok(d.msgs.has('FE48'), 'berichten blijven');
});
test('een ander toestel neemt een adres over', () => {
  const m = new Model();
  m.push([F(0, 6, 0xee00, 0x80, 0xff, nameBytes({ fn: 130, identity: 5 }))]);
  const old = m.devices.get(0x80);
  m.push([F(1, 6, 0xee00, 0x80, 0xff, nameBytes({ fn: 29, identity: 6 }))]);
  ok(m.devices.get(0x80) !== old, 'nieuw toestel');
  ok(old.lost, 'oude is adres kwijt');
  eq(m.deviceList().length, 2);
});
test('DM1 in de diagnosegroep, en weg als de storing weg is', () => {
  const m = new Model();
  m.push([F(0, 6, 0xfeca, 0x80, 0xff, [0x04, 0xff, 0x00, 0xf0, 0xff, 0x03, 0xff, 0xff])]);
  const D = m.devices.get(0x80).diag;
  ok(D.items.has('SPN520192.31'), 'storing aanwezig');
  m.push([F(1, 6, 0xfeca, 0x80, 0xff, [0x00, 0xff, 0, 0, 0, 0, 0xff, 0xff])]);
  ok(!D.items.has('SPN520192.31'), 'storing weg');
  eq(D.items.get('count').value, 0);
});
test('volglijst overleeft een reset (stabiele sleutel)', () => {
  const m = new Model();
  const frames = [F(0, 6, 0xee00, 0xf0, 0xff, nameBytes({ fn: 134 })), F(0.1, 3, 0xfe48, 0xf0, 0xff, [0, 0, 0, 0, 0, 0, 0xff, 0xff])];
  m.push(frames);
  m.setWatch(v(m, 0xf0, 'FE48', 'Snelheid'), true);
  m.reset();
  m.push(frames);
  eq([...m.watch].map((x) => x.path), ['F0/FE48/Snelheid']);
});
test('11-bit frames onder een eigen knooppunt', () => {
  const m = new Model();
  m.push([frame(0, 0x123, [1, 2], false)]);
  eq(m.std.msgs.get('123').data.length, 2);
  eq(m.deviceList().at(-1).label, '11-bit berichten');
});

section('logbestanden');
const SAMPLE = {
  microchip: 'Tijd;Richting;ID;DLC;Data\n0,100;Rx;18FEF100x;8;01;02;03;04;05;06;07;08\n0,200;Rx;123;2;AA;BB\n',
  candumpL: '(1690000000.500000) can0 18FEF100#0102\n(1690000000.600000) can0 123#AABB\n',
  candump: '  can0  18FEF100   [8]  01 02 03 04 05 06 07 08\n  can0  123   [2]  AA BB\n',
  trc11: ';$FILEVERSION=1.1\n;   Message Number\n     1)      1059.9  Rx     18FEF100  8  01 02 03 04 05 06 07 08\n     2)      1060.0  Rx         0123  2  AA BB\n',
  trc21: ';$FILEVERSION=2.1\n      1      1059.900 DT     1      18FEF100 Rx -  8    01 02 03 04 05 06 07 08\n      2      1060.000 DT     1      0123 Rx -  2    AA BB\n',
  csv: 'tijd;richting;id;pgn;bron;doel;data\n12.3456;rx;18FEF100;0FEF1;00;FF;0102030405060708\n12.5;tx;123;00100;23;FF;aabb\n',
};
for (const [k, txt] of Object.entries(SAMPLE)) {
  test(`${k}: twee frames, 29- en 11-bit`, () => {
    const r = parseLog(txt);
    eq(r.frames.length, 2, 'aantal');
    eq([r.frames[0].id, r.frames[0].ext, r.frames[1].id, r.frames[1].ext], [0x18fef100, true, 0x123, false]);
    eq(Array.from(r.frames[1].data), [0xaa, 0xbb]);
  });
}
test('formaatherkenning', () => eq(['microchip', 'candumpL', 'trc11', 'trc21', 'csv'].map((k) => detect(SAMPLE[k])), ['microchip', 'candump', 'trc', 'trc', 'csv']));
test('candump-regel schrijven en terug inlezen', () => {
  const f = frame(1.5, 0x18fef100, [1, 2, 3], true);
  const r = parseLog(toCandump(f) + '\n');
  eq([r.frames[0].t, r.frames[0].id, Array.from(r.frames[0].data)], [1.5, 0x18fef100, [1, 2, 3]]);
});
