// CAN frames as every source delivers them, and the J1939 / ISO 11783 view of a 29-bit identifier.
//
// A frame: { t, id, ext, data, dir }
//   t     time in seconds (Date.now() / 1000 for live sources, the log time for a file)
//   id    identifier (11 or 29 bit)
//   ext   true for a 29-bit identifier
//   data  Uint8Array (0..8 bytes)
//   dir   'rx' (received) or 'tx' (sent by the simulator itself)

export const GLOBAL = 0xff;
export const NULL_ADDR = 0xfe;

export const PGN = {
  ACK: 0xe800, REQUEST: 0xea00, ADDRESS_CLAIMED: 0xee00, TP_CM: 0xec00, TP_DT: 0xeb00,
  ETP_CM: 0xc800, ETP_DT: 0xc700, PROCESS_DATA: 0xcb00, VT_TO_ECU: 0xe600, ECU_TO_VT: 0xe700,
  DM1: 0xfeca, SOFTWARE_ID: 0xfeda, ECU_ID: 0xfdc5,
};

/** → { prio, pgn, sa, da } of a 29-bit identifier (PDU1: the destination is taken out of the PGN). */
export function splitId(id) {
  const prio = (id >>> 26) & 7;
  let pgn = (id >>> 8) & 0x3ffff;
  const pf = (pgn >> 8) & 0xff;
  const sa = id & 0xff;
  let da = GLOBAL;
  if (pf < 240) { da = pgn & 0xff; pgn &= 0x3ff00; }
  return { prio, pgn, sa, da };
}

export function makeId(prio, pgn, sa, da = GLOBAL) {
  if (((pgn >> 8) & 0xff) < 240) pgn = (pgn & 0x3ff00) | (da & 0xff);
  return (((prio & 7) << 26) | ((pgn & 0x3ffff) << 8) | (sa & 0xff)) >>> 0;
}

export const isPdu1 = (pgn) => ((pgn >> 8) & 0xff) < 240;

export const hex = (v, n = 2) => (v === null || v === undefined ? '—' : Number(v).toString(16).toUpperCase().padStart(n, '0'));
export const hexBytes = (d) => Array.from(d, (b) => b.toString(16).toUpperCase().padStart(2, '0')).join(' ');

export function frame(t, id, data, ext = id > 0x7ff, dir = 'rx') {
  return { t, id: id >>> 0, ext, data: data instanceof Uint8Array ? data : Uint8Array.from(data), dir };
}

/** Bits on the bus for the load estimate (same rule as the server: 64 + 8 per data byte). */
export const frameBits = (f) => 64 + 8 * f.data.length;
