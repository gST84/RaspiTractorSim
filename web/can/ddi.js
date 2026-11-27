// Data dictionary entities (ISO 11783-11): name, unit and resolution of a DDI. Only what the analyzer needs
// when the DDOP of an implement is not known; with a DDOP the designator and value presentation of the
// implement itself win. Sources: PLC-VT-Designer src/tc/ddi-setpoints.js (setpoint/actual pairs from the
// isobus.net data base, version 2026101901) and the structural DDIs of src/tc/build.js.

const T = new Map();
const add = (ddi, name, unit = '', res = 1, kind = 'num') => T.set(ddi, { ddi, name, unit, res, kind });

// setpoint / actual pairs
const PAIRS = [
  [1, 2, 'Volume Per Area Application Rate', 'mm³/m²', 0.01], [6, 7, 'Mass Per Area Application Rate', 'mg/m²', 1],
  [11, 12, 'Count Per Area Application Rate', '/m²', 0.001], [16, 17, 'Spacing Application Rate', 'mm', 1],
  [21, 22, 'Volume Per Volume Application Rate', 'mm³/m³', 1], [26, 27, 'Mass Per Mass Application Rate', 'mg/kg', 1],
  [31, 32, 'Volume Per Mass Application Rate', 'mm³/kg', 1], [36, 37, 'Volume Per Time Application Rate', 'mm³/s', 1],
  [41, 42, 'Mass Per Time Application Rate', 'mg/s', 1], [46, 47, 'Count Per Time Application Rate', '/s', 0.001],
  [51, 52, 'Tillage Depth', 'mm', 1], [56, 57, 'Seeding Depth', 'mm', 1], [61, 62, 'Working Height', 'mm', 1],
  [101, 102, 'Bale Width', 'mm', 1], [106, 107, 'Bale Height', 'mm', 1], [111, 112, 'Bale Size', 'mm', 1],
  [140, 308, 'Percentage Application Rate', 'ppm', 1], [193, 194, 'Product Pressure', 'Pa', 0.1],
  [197, 198, 'Pump Output Pressure', 'Pa', 0.1], [201, 202, 'Tank Agitation Pressure', 'Pa', 0.1],
  [218, 219, 'Bale Compression Plunger Load', 'ppm', 1], [225, 226, 'Working Length', 'mm', 1],
  [242, 243, 'Sieve Clearance', 'mm', 1], [246, 247, 'Chaffer Clearance', 'mm', 1], [250, 251, 'Concave Clearance', 'mm', 1],
  [254, 255, 'Separation Fan Rotational Speed', '/s', 0.001], [326, 327, 'Header Speed', '/s', 0.001],
  [330, 331, 'Cutting drum speed', '/s', 0.001], [341, 342, 'Grain Kernel Cracker Gap', 'mm', 0.001],
  [345, 346, 'Swathing Width', 'mm', 1], [365, 366, 'Downforce Pressure', 'Pa', 1], [396, 397, 'Speed', 'mm/s', 1],
  [426, 427, 'Downforce as Force', 'N', 1], [432, 433, 'Application Rate of Nitrogen', 'mg/m²', 1],
  [436, 437, 'Application Rate of Ammonium', 'mg/m²', 1], [440, 441, 'Application Rate of Phosphor', 'mg/m²', 1],
  [444, 445, 'Application Rate of Potassium', 'mg/m²', 1], [448, 449, 'Application Rate of Dry Matter', 'ppm', 1],
  [475, 216, 'Bale Hydraulic Pressure', 'Pa', 1], [478, 364, 'Flake Size', 'mm', 1], [483, 484, 'Engine Speed', 'r/min', 0.0001],
  [501, 502, 'Engine Torque', '%', 0.001], [529, 530, 'Tillage Disc Gang Angle', '°', 0.001],
  [532, 531, 'Applied Preservative Per Yield Mass', 'mm³/kg', 1], [542, 541, 'PTO Speed', 'r/min', 0.0001],
  [547, 548, 'Bale Compression Plunger Load (N)', 'N', 1], [552, 551, 'PTO Torque', 'N*m', 0.0001],
  [557, 558, 'Electrical Current', 'A', 0.005], [562, 564, 'Voltage', 'V', 0.001], [568, 569, 'Electrical Power', 'W', 0.001],
  [574, 575, 'Electrical Energy per Area Application Rate', 'kWh/m²', 1e-7], [578, 579, 'Temperature', 'mK', 1],
  [583, 584, 'Frequency', 'Hz', 0.001], [588, 589, 'Volume Per Area Application Rate [ml/m²]', 'ml/m²', 0.1],
  [633, 634, 'Volume per distance Application Rate', 'ml/m', 0.001], [638, 639, 'Tire Pressure', 'Pa', 0.1],
];
for (const [sp, act, name, unit, res] of PAIRS) { add(sp, `Setpoint ${name}`, unit, res); add(act, `Actual ${name}`, unit, res); }

// structure of an implement
add(67, 'Actual Working Width', 'mm', 1);
add(72, 'Actual Volume Content', 'ml', 1); add(73, 'Maximum Volume Content', 'ml', 1);
add(75, 'Actual Mass Content', 'g', 1); add(76, 'Maximum Mass Content', 'g', 1);
add(80, 'Total Volume', 'L', 1); add(81, 'Total Mass', 'kg', 1); add(82, 'Total Count', '', 1);
add(116, 'Total Area', 'm²', 1); add(119, 'Effective Total Time', 's', 1);
add(134, 'Device Element Offset X', 'mm', 1); add(135, 'Device Element Offset Y', 'mm', 1); add(136, 'Device Element Offset Z', 'mm', 1);
add(141, 'Actual Work State', '', 1, 'state');
add(157, 'Connector Type', '', 1, 'connector');
add(158, 'Prescription Control State', '', 1, 'state');
add(160, 'Section Control State', '', 1, 'state');
for (let i = 0; i < 16; i++) add(161 + i, `Actual Condensed Work State (${i * 16 + 1}-${i * 16 + 16})`, '', 1, 'condensed');
add(179, 'Actual Cultural Practice', '', 1, 'practice');
add(289, 'Setpoint Work State', '', 1, 'state');
for (let i = 0; i < 16; i++) add(290 + i, `Setpoint Condensed Work State (${i * 16 + 1}-${i * 16 + 16})`, '', 1, 'condensed');
add(57343, 'PGN Based Data / Request Default Process Data', '', 1);

const STATE = { 0: 'uit', 1: 'aan', 2: 'fout', 3: 'n.b.' };
const CONNECTOR = { 0: 'onbekend', 1: 'trekhaak / trekbalk', 2: 'driepuntshef halfgedragen', 3: 'driepuntshef gedragen', 4: 'haak', 5: 'trekoog 40',
  6: 'piton', 7: 'CUNA', 8: 'kogel K80', 9: 'zelfrijdend', 10: 'draaikrans' };
const PRACTICE = { 0: 'onbekend', 1: 'bemesten', 2: 'zaaien en planten', 3: 'gewasbescherming', 4: 'grondbewerking', 5: 'persen', 6: 'maaien',
  7: 'wikkelen', 8: 'oogsten', 9: 'hakselen', 10: 'transport', 11: 'zwaden', 12: 'mest uitrijden', 13: 'opraapwagen', 14: 'schudden',
  15: 'meten', 16: 'irrigatie', 17: 'voeren / mengen', 18: 'mulchen' };

/** { ddi, name, unit, res, kind } for a DDI (a generic entry for unknown and proprietary ones). */
export function ddiInfo(ddi) {
  const e = T.get(ddi);
  if (e) return e;
  if (ddi >= 57344 && ddi <= 65534) return { ddi, name: `Eigen DDI ${ddi}`, unit: '', res: 1, kind: 'num', proprietary: true };
  return { ddi, name: `DDI ${ddi}`, unit: '', res: 1, kind: 'num', unknown: true };
}

/** Text of a value of a state-like DDI, or null for a plain number. */
export function ddiText(info, raw) {
  switch (info.kind) {
    case 'state': return STATE[raw & 3] ?? String(raw);
    case 'connector': return CONNECTOR[raw] ?? String(raw);
    case 'practice': return PRACTICE[raw] ?? String(raw);
    case 'condensed': { // 16 sections × 2 bits, section 1 in the lowest bits
      const s = [];
      for (let i = 0; i < 16; i++) { const v = (raw >>> (2 * i)) & 3; if (v !== 3) s.push(v === 1 ? '■' : v === 0 ? '□' : '!'); }
      return s.length ? s.join('') : 'n.b.';
    }
    default: return null;
  }
}
