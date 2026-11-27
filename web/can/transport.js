// Reassembles long messages while listening on the bus (ISO 11783-3 / SAE J1939-21 and NMEA 2000):
//   TP   ≤ 1785 bytes: BAM (broadcast) and RTS/CTS (destination specific)
//   ETP  > 1785 bytes: RTS / CTS / DPO, destination specific only
//   fast packet: NMEA 2000, ≤ 223 bytes, sequence counter in the first byte
// The listener does not take part: a message is complete as soon as all its data packets were seen.

import { PGN } from './frames.js';

/** NMEA 2000 PGNs that are sent as fast packet (the ones a tractor or GNSS receiver uses). */
export const FAST_PACKET = new Set([
  126208, 126464, 126720, 126996, 126998, 127233, 127237, 127489, 127496, 127497, 127498, 127503, 127504, 127506,
  127507, 127509, 127510, 127511, 127512, 127513, 127514, 128275, 128520, 129029, 129038, 129039, 129040, 129041,
  129044, 129045, 129284, 129285, 129301, 129302, 129538, 129540, 129541, 129542, 129545, 129547, 129549, 129551,
  129556, 129792, 129793, 129794, 129795, 129796, 129797, 129798, 129799, 129800, 129801, 129802, 129803, 129804,
  129805, 129806, 129807, 129808, 129809, 129810, 130052, 130053, 130054, 130060, 130061, 130064, 130065, 130066,
  130067, 130068, 130069, 130070, 130071, 130072, 130073, 130074, 130320, 130321, 130322, 130323, 130324, 130569,
  130570, 130571, 130572, 130573, 130574, 130577, 130578, 130816,
]);

const TIMEOUT = 3; // s without a data packet before a session is dropped

const u24 = (d, i) => d[i] | (d[i + 1] << 8) | (d[i + 2] << 16);

class Session {
  constructor(kind, sa, da, pgn, size, t) {
    this.kind = kind; this.sa = sa; this.da = da; this.pgn = pgn; this.size = size;
    this.total = Math.ceil(size / 7);
    this.buf = new Uint8Array(this.total * 7).fill(0xff);
    this.seen = new Uint8Array(this.total);
    this.count = 0;
    this.offset = 0; // ETP: packet offset of the current DPO
    this.t = t;
  }

  put(n, bytes) { // n: 1-based packet number of the whole message
    if (n < 1 || n > this.total) return false;
    this.buf.set(bytes.subarray(0, 7), (n - 1) * 7);
    if (!this.seen[n - 1]) { this.seen[n - 1] = 1; this.count++; }
    return this.count === this.total;
  }
}

export class Transport {
  /** onMessage({ t, pgn, sa, da, prio, data, via }) receives every complete message, short ones included. */
  constructor(onMessage) {
    this.onMessage = onMessage;
    this.sessions = new Map(); // 'tp:sa:da' / 'etp:sa:da' → Session
    this.fast = new Map(); // 'sa:pgn' → { seq, size, buf, got, next }
    this.stats = { tp: 0, etp: 0, fast: 0, aborted: 0, dropped: 0 };
  }

  reset() { this.sessions.clear(); this.fast.clear(); }

  /** Live sessions, for the bus node: [{ kind, sa, da, pgn, size, done }] */
  active() {
    return [...this.sessions.values()].map((s) => ({ kind: s.kind, sa: s.sa, da: s.da, pgn: s.pgn, size: s.size, done: s.count * 7 }));
  }

  /** One 29-bit frame (id: the CAN identifier, passed on with single-frame messages for DBC matching). */
  push(t, prio, pgn, sa, da, data, id) {
    // the transport frames themselves are messages too (their own node in the tree), before the result
    switch (pgn) {
      case PGN.TP_CM: this.onMessage({ t, prio, pgn, sa, da, data, id, via: '' }); this.tpCm(t, sa, da, data); break;
      case PGN.TP_DT: this.onMessage({ t, prio, pgn, sa, da, data, id, via: '' }); this.dt(t, 'tp', sa, da, data); break;
      case PGN.ETP_CM: this.onMessage({ t, prio, pgn, sa, da, data, id, via: '' }); this.etpCm(t, sa, da, data); break;
      case PGN.ETP_DT: this.onMessage({ t, prio, pgn, sa, da, data, id, via: '' }); this.dt(t, 'etp', sa, da, data); break;
      default:
        if (FAST_PACKET.has(pgn)) this.fastPacket(t, prio, pgn, sa, da, data);
        else this.onMessage({ t, prio, pgn, sa, da, data, id, via: '' });
    }
    if (this.sessions.size) this.expire(t);
  }

  expire(t) {
    for (const [k, s] of this.sessions) if (t - s.t > TIMEOUT) { this.sessions.delete(k); this.stats.dropped++; }
  }

  tpCm(t, sa, da, d) {
    const c = d[0];
    if (c === 16 || c === 32) { // RTS or BAM: a new message from sa
      const s = new Session('tp', sa, c === 32 ? 0xff : da, u24(d, 5), d[1] | (d[2] << 8), t);
      this.sessions.set(`tp:${sa}:${s.da}`, s);
    } else if (c === 255) { // abort, from either side
      if (this.sessions.delete(`tp:${sa}:${da}`) || this.sessions.delete(`tp:${da}:${sa}`)) this.stats.aborted++;
    }
  }

  etpCm(t, sa, da, d) {
    const c = d[0];
    if (c === 20) { // RTS
      const size = (d[1] | (d[2] << 8) | (d[3] << 16) | (d[4] << 24)) >>> 0;
      if (size > 117440505) return;
      this.sessions.set(`etp:${sa}:${da}`, new Session('etp', sa, da, u24(d, 5), size, t));
    } else if (c === 22) { // DPO: data packet offset for the next DT packets
      const s = this.sessions.get(`etp:${sa}:${da}`);
      if (s) { s.offset = u24(d, 2); s.t = t; }
    } else if (c === 255) {
      if (this.sessions.delete(`etp:${sa}:${da}`) || this.sessions.delete(`etp:${da}:${sa}`)) this.stats.aborted++;
    }
  }

  dt(t, kind, sa, da, d) {
    const key = `${kind}:${sa}:${kind === 'tp' && !this.sessions.has(`tp:${sa}:${da}`) ? 0xff : da}`;
    const s = this.sessions.get(key);
    if (!s || d.length < 2) return;
    s.t = t;
    const n = kind === 'etp' ? s.offset + d[0] : d[0];
    if (s.put(n, d.subarray(1))) {
      this.sessions.delete(key);
      this.stats[kind]++;
      this.onMessage({ t, prio: 7, pgn: s.pgn, sa: s.sa, da: s.da, data: s.buf.slice(0, s.size), via: kind.toUpperCase() });
    }
  }

  fastPacket(t, prio, pgn, sa, da, d) {
    if (d.length < 2) return;
    const key = `${sa}:${pgn}`;
    const seq = d[0] >> 5, n = d[0] & 0x1f;
    if (n === 0) {
      const size = d[1];
      const f = { seq, size, buf: new Uint8Array(Math.max(size, 6)), got: Math.min(6, size), next: 1, t };
      f.buf.set(d.subarray(2, 2 + Math.min(6, size)));
      if (f.got >= size) { this.stats.fast++; this.onMessage({ t, prio, pgn, sa, da, data: f.buf.slice(0, size), via: 'FP' }); return; }
      this.fast.set(key, f);
      return;
    }
    const f = this.fast.get(key);
    if (!f || f.seq !== seq || f.next !== n) { if (f && f.seq === seq) this.fast.delete(key); return; }
    const take = Math.min(7, f.size - f.got);
    f.buf.set(d.subarray(1, 1 + take), f.got);
    f.got += take;
    f.next++;
    if (f.got >= f.size) {
      this.fast.delete(key);
      this.stats.fast++;
      this.onMessage({ t, prio, pgn, sa, da, data: f.buf.slice(0, f.size), via: 'FP' });
    }
  }
}
