// Device descriptor object pool (DDOP, ISO 11783-10): the description of an implement that a TC client sends
// to the task controller. Parser taken from PLC-VT-Designer src/tc/ddop.js, extended with the version 4 fields
// of the DVC (extended structure label) so pools of other implements parse too.
//   DVC device           designator, software version, NAME (8), serial number, structure label (7),
//                        localization label (7) [, extended structure label (length + bytes), version 4]
//   DET device element   type, designator, element number, parent id, child ids
//   DPD process data     DDI, properties, trigger methods, designator, presentation id
//   DPT property         DDI, value (int32), designator, presentation id
//   DVP presentation     offset (int32), scale (float32), decimals, unit designator

export const NONE = 0xffff;
export const DET_TYPES = { 1: 'toestel', 2: 'functie', 3: 'bak / product', 4: 'sectie', 5: 'eenheid', 6: 'koppeling', 7: 'navigatiereferentie' };
const TABLES = new Set(['DVC', 'DET', 'DPD', 'DPT', 'DVP']);

/** Objects from pool bytes (throws on malformed data). */
export function parseDdop(bytes) {
  const b = bytes instanceof Uint8Array ? bytes : Uint8Array.from(bytes);
  let p = 0;
  const need = (n) => { if (p + n > b.length) throw new Error(`DDOP afgebroken bij byte ${p}`); };
  const u8 = () => { need(1); return b[p++]; };
  const u16 = () => u8() | (u8() << 8);
  const i32 = () => (u16() | (u16() << 16)) | 0;
  const f32 = () => { need(4); const v = new DataView(b.buffer, b.byteOffset + p, 4).getFloat32(0, true); p += 4; return v; };
  const raw = (n) => { need(n); const r = Array.from(b.subarray(p, p + n)); p += n; return r; };
  const text = () => String.fromCharCode(...raw(u8()));
  const peekTable = () => (p + 3 <= b.length ? String.fromCharCode(b[p], b[p + 1], b[p + 2]) : '');
  const out = [];
  while (p < b.length) {
    const table = String.fromCharCode(...raw(3));
    const id = u16();
    switch (table) {
      case 'DVC': {
        const designator = text(), softwareVersion = text(), name = raw(8), serial = text();
        const o = { table, id, designator, softwareVersion, name, serial, structureLabel: raw(7), localization: raw(7) };
        if (p < b.length && !TABLES.has(peekTable())) o.extendedLabel = raw(u8()); // version 4
        out.push(o);
        break;
      }
      case 'DET': {
        const type = u8(), designator = text(), number = u16(), parent = u16(), n = u16();
        const children = [];
        for (let i = 0; i < n; i++) children.push(u16());
        out.push({ table, id, type, designator, number, parent, children });
        break;
      }
      case 'DPD': out.push({ table, id, ddi: u16(), props: u8(), triggers: u8(), designator: text(), presentation: u16() }); break;
      case 'DPT': out.push({ table, id, ddi: u16(), value: i32(), designator: text(), presentation: u16() }); break;
      case 'DVP': out.push({ table, id, offset: i32(), scale: f32(), decimals: u8(), unit: text() }); break;
      default: throw new Error(`Onbekend DDOP-object "${table}" bij byte ${p - 5}`);
    }
  }
  return out;
}

/** For tests: the binary form of DDOP objects (version 3), as PLC-VT-Designer writes it. */
export function encodeDdop(objects) {
  const w = [];
  const u8 = (v) => w.push(v & 255);
  const u16 = (v) => { u8(v); u8(v >> 8); };
  const i32 = (v) => { u16(v); u16(v >> 16); };
  const text = (s) => { u8(s.length); for (const c of s) u8(c.charCodeAt(0)); };
  for (const o of objects) {
    for (const c of o.table) u8(c.charCodeAt(0));
    u16(o.id);
    if (o.table === 'DVC') { text(o.designator); text(o.softwareVersion); o.name.forEach(u8); text(o.serial); o.structureLabel.forEach(u8); o.localization.forEach(u8); }
    if (o.table === 'DET') { u8(o.type); text(o.designator); u16(o.number); u16(o.parent); u16(o.children.length); o.children.forEach(u16); }
    if (o.table === 'DPD') { u16(o.ddi); u8(o.props); u8(o.triggers); text(o.designator); u16(o.presentation ?? NONE); }
    if (o.table === 'DPT') { u16(o.ddi); i32(o.value); text(o.designator); u16(o.presentation ?? NONE); }
    if (o.table === 'DVP') { i32(o.offset); const a = new Uint8Array(4); new DataView(a.buffer).setFloat32(0, o.scale, true); a.forEach(u8); u8(o.decimals); text(o.unit); }
  }
  return Uint8Array.from(w);
}
