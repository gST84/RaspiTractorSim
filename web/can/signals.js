// A signal inside the data of a message, in the same form for the built-in ISOBUS/J1939 messages and for
// signals from a DBC file:
//
//   { name, start, len, order, signed, scale, offset, unit, values, type, na, min, max, comment, mux }
//   start   Intel ('le'): bit number of the least significant bit (byte * 8 + bit, bit 0 = LSB)
//           Motorola ('be'): bit number of the most significant bit, DBC numbering (byte * 8 + bit)
//   type    'num' (default), 'enum', 'text' (ASCII bytes), 'hex', 'float' (IEEE 754, 32 or 64 bit)
//   values  { raw: text } for an enumeration
//   na      'j1939': "not available" / "error" ranges of SAE J1939-71 and ISO 11783-7
//           'n2k':   NMEA 2000 (all ones = not available, all ones - 1 = out of range)
//   mux     { by: name of the multiplexer signal, value } for a multiplexed DBC signal

/** Raw unsigned value: a Number up to 32 bits, a BigInt above. */
export function rawBits(d, start, len, order = 'le') {
  if (order === 'be') return beBits(d, start, len);
  if (len <= 32) {
    if ((start & 7) === 0 && (len & 7) === 0) {
      let v = 0;
      for (let i = (len >> 3) - 1; i >= 0; i--) v = v * 256 + d[(start >> 3) + i];
      return v;
    }
    let v = 0;
    for (let i = len - 1; i >= 0; i--) {
      const b = start + i;
      v = v * 2 + ((d[b >> 3] >> (b & 7)) & 1);
    }
    return v;
  }
  let v = 0n;
  for (let i = len - 1; i >= 0; i--) {
    const b = start + i;
    v = (v << 1n) | BigInt((d[b >> 3] >> (b & 7)) & 1);
  }
  return v;
}

function beBits(d, start, len) {
  let pos = start;
  if (len <= 32) {
    let v = 0;
    for (let i = 0; i < len; i++) {
      v = v * 2 + ((d[pos >> 3] >> (pos & 7)) & 1);
      pos = (pos & 7) === 0 ? pos + 15 : pos - 1;
    }
    return v;
  }
  let v = 0n;
  for (let i = 0; i < len; i++) {
    v = (v << 1n) | BigInt((d[pos >> 3] >> (pos & 7)) & 1);
    pos = (pos & 7) === 0 ? pos + 15 : pos - 1;
  }
  return v;
}

/** Highest byte index a signal touches (to see whether the data is long enough). */
export function lastByte(sig) {
  if (sig.order === 'be') {
    let pos = sig.start;
    for (let i = 1; i < sig.len; i++) pos = (pos & 7) === 0 ? pos + 15 : pos - 1;
    return Math.max(sig.start >> 3, pos >> 3);
  }
  return (sig.start + sig.len - 1) >> 3;
}

function status(sig, raw) {
  const len = sig.len;
  if (sig.na === 'j1939') {
    const r = typeof raw === 'bigint' ? raw : BigInt(raw);
    if (len === 1) return 'ok';
    if (len === 2) return r === 3n ? 'na' : r === 2n ? 'err' : 'ok';
    if (len === 4) return r === 15n ? 'na' : r === 14n ? 'err' : 'ok';
    if (len % 8 === 0) {
      const top = Number(r >> BigInt(len - 8));
      return top === 0xff ? 'na' : top === 0xfe ? 'err' : top >= 0xfb ? 'na' : 'ok';
    }
    return r === (1n << BigInt(len)) - 1n ? 'na' : 'ok';
  }
  if (sig.na === 'n2k') {
    const r = typeof raw === 'bigint' ? raw : BigInt(raw);
    if (len < 2) return 'ok';
    const max = sig.signed ? (1n << BigInt(len - 1)) - 1n : (1n << BigInt(len)) - 1n;
    return r === max ? 'na' : r === max - 1n && len >= 8 ? 'err' : 'ok';
  }
  return 'ok';
}

const latin = (bytes) => String.fromCharCode(...bytes).replace(/[\0\xff]+$/g, '').replace(/[\x00-\x1f]/g, '·');

/**
 * Decodes one signal → { raw, value, text, status }.
 * value is a Number (scaled), a string for text signals, or null when not available / in error.
 * status: 'ok', 'na' (not available), 'err' (error indicator) or 'short' (message too short).
 */
export function decodeSignal(d, sig) {
  if (lastByte(sig) >= d.length) return { raw: null, value: null, text: '—', status: 'short' };
  const type = sig.type || (sig.values ? 'enum' : 'num');
  if (type === 'text') {
    const a = sig.start >> 3;
    const s = latin(d.subarray(a, a + (sig.len >> 3)));
    return { raw: null, value: s, text: s, status: 'ok' };
  }
  if (type === 'float') {
    const dv = new DataView(d.buffer, d.byteOffset + (sig.start >> 3), sig.len >> 3);
    const v = sig.len === 64 ? dv.getFloat64(0, sig.order !== 'be') : dv.getFloat32(0, sig.order !== 'be');
    const value = v * (sig.scale ?? 1) + (sig.offset ?? 0);
    return { raw: v, value, text: fmtNum(value, sig), status: Number.isFinite(v) ? 'ok' : 'na' };
  }
  let raw = rawBits(d, sig.start, sig.len, sig.order);
  const st = status(sig, raw);
  let n = raw;
  if (sig.signed) {
    if (typeof raw === 'bigint') { if (raw >> BigInt(sig.len - 1)) n = raw - (1n << BigInt(sig.len)); }
    else if (raw >= 2 ** (sig.len - 1)) n = raw - 2 ** sig.len;
  }
  if (type === 'hex') {
    const s = (typeof raw === 'bigint' ? raw : BigInt(raw)).toString(16).toUpperCase().padStart(Math.ceil(sig.len / 4), '0');
    return { raw, value: st === 'ok' ? s : null, text: st === 'ok' ? s : naText(st), status: st };
  }
  const num = typeof n === 'bigint' ? Number(n) : n;
  if (type === 'enum' || sig.values) {
    const key = typeof raw === 'bigint' ? raw.toString() : raw;
    const label = sig.values?.[key];
    if (label !== undefined) return { raw, value: num, text: label, status: st };
    if (type === 'enum') return { raw, value: st === 'ok' ? num : null, text: st === 'ok' ? String(num) : naText(st), status: st };
  }
  if (st !== 'ok') return { raw, value: null, text: naText(st), status: st };
  const value = num * (sig.scale ?? 1) + (sig.offset ?? 0);
  return { raw, value, text: fmtNum(value, sig), status: 'ok' };
}

export const naText = (st) => ({ na: 'n.b.', err: 'fout', short: '—' }[st] || '—');

/** Number with as many decimals as the resolution needs. */
export function fmtNum(v, sig) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const step = Math.abs(sig?.scale ?? 1) || 1;
  const dec = sig?.decimals ?? (step >= 1 ? 0 : Math.min(7, Math.ceil(-Math.log10(step) - 1e-9)));
  return v.toLocaleString('nl-BE', { minimumFractionDigits: dec, maximumFractionDigits: dec, useGrouping: Math.abs(v) >= 1e5 });
}

/** Describes where a signal sits: "byte 3-4" or "byte 8 bit 3-4" (1-based, as in ISO 11783-7). */
export function position(sig) {
  if (sig.order === 'be') return `startbit ${sig.start} (Motorola), ${sig.len} bit`;
  const a = sig.start >> 3, b = (sig.start + sig.len - 1) >> 3;
  if ((sig.start & 7) === 0 && (sig.len & 7) === 0) return a === b ? `byte ${a + 1}` : `byte ${a + 1}-${b + 1}`;
  const lo = (sig.start & 7) + 1;
  return a === b ? `byte ${a + 1} bit ${lo + sig.len - 1}-${lo}`.replace(/bit (\d+)-\1$/, 'bit $1') : `bit ${sig.start}-${sig.start + sig.len - 1}`;
}
