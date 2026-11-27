// DBC files (Vector CANdb++): messages and signals for machines and proprietary messages the built-in
// ISOBUS/J1939 database does not know.
//   BO_ id name: dlc sender                     message (bit 31 of id = 29-bit identifier)
//   SG_ name [M|mN] : start|len@order sign (scale,offset) [min|max] "unit" receivers
//   VAL_ id signal value "text" … ;            value table
//   CM_ BO_/SG_ … "comment";                     comments
//   SIG_VALTYPE_ id signal : 1|2;                float / double
//   BA_ "VFrameFormat" BO_ id 3;                 J1939PG: matched on the PGN, whatever the source address
//   BA_ "ProtocolType" "J1939";                  every 29-bit message of the file is J1939
// Order 1 = Intel (little endian), 0 = Motorola (big endian, start = most significant bit).

/** DBC text → { messages: [def], byId: Map('x'|'s' + id → def), byPgn: Map(pgn → def), warnings } */
export function parseDbc(text) {
  const src = text.replace(/\r\n?/g, '\n');
  const messages = [];
  const byKey = new Map(); // dbc id → message
  const warnings = [];
  let cur = null;
  const frameFormats = []; // enum values of VFrameFormat
  const msgFormat = new Map(); // dbc id → enum index
  let protocolJ1939 = false;

  const lines = src.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    let m;
    if ((m = /^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)\s+(\w+)/.exec(line))) {
      const raw = Number(m[1]);
      if (raw === 0xc0000000) { cur = null; continue; } // VECTOR__INDEPENDENT_SIG_MSG
      const ext = raw >= 0x80000000 || (raw & 0x1fffffff) > 0x7ff;
      cur = { dbcId: raw, id: raw & 0x1fffffff, ext, name: m[2], dlc: Number(m[3]), sender: m[4], signals: [], comment: '' };
      messages.push(cur);
      byKey.set(raw, cur);
    } else if ((m = /^SG_\s+(\w+)\s*(M|m\d+M?)?\s*:\s*(\d+)\|(\d+)@([01])([+-])\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)\s*\[\s*([^|]*)\|([^\]]*)\]\s*"([^"]*)"/.exec(line))) {
      if (!cur) continue;
      const s = {
        name: m[1], start: Number(m[3]), len: Number(m[4]), order: m[5] === '1' ? 'le' : 'be', signed: m[6] === '-',
        scale: Number(m[7]), offset: Number(m[8]), min: Number(m[9]), max: Number(m[10]), unit: m[11], na: null, source: 'DBC',
      };
      if (m[2] === 'M') s.isMux = true;
      else if (m[2]) { s.mux = { by: null, value: parseInt(m[2].slice(1), 10) }; if (m[2].endsWith('M')) s.isMux = true; }
      cur.signals.push(s);
    } else if (line.startsWith('VAL_ ')) {
      let stmt = line;
      while (!/;\s*$/.test(stmt) && i + 1 < lines.length) stmt += ' ' + lines[++i].trim();
      const h = /^VAL_\s+(\d+)\s+(\w+)\s+(.*);\s*$/.exec(stmt);
      if (!h) continue;
      const sig = byKey.get(Number(h[1]))?.signals.find((x) => x.name === h[2]);
      if (!sig) continue;
      const values = {};
      for (const v of h[3].matchAll(/(-?\d+)\s+"([^"]*)"/g)) values[v[1]] = v[2];
      sig.values = values;
      sig.type = 'enum';
    } else if (line.startsWith('CM_ ')) {
      let stmt = line;
      while (!/"\s*;\s*$/.test(stmt) && i + 1 < lines.length) stmt += '\n' + lines[++i];
      let c;
      if ((c = /^CM_\s+SG_\s+(\d+)\s+(\w+)\s+"([\s\S]*)"\s*;\s*$/.exec(stmt))) {
        const sig = byKey.get(Number(c[1]))?.signals.find((x) => x.name === c[2]);
        if (sig) sig.comment = c[3].trim();
      } else if ((c = /^CM_\s+BO_\s+(\d+)\s+"([\s\S]*)"\s*;\s*$/.exec(stmt))) {
        const msg = byKey.get(Number(c[1]));
        if (msg) msg.comment = c[2].trim();
      }
    } else if ((m = /^SIG_VALTYPE_\s+(\d+)\s+(\w+)\s*:\s*([12])\s*;/.exec(line))) {
      const sig = byKey.get(Number(m[1]))?.signals.find((x) => x.name === m[2]);
      if (sig) { sig.type = 'float'; if (m[3] === '2' && sig.len !== 64) warnings.push(`${m[2]}: double van ${sig.len} bit`); }
    } else if ((m = /^BA_DEF_\s+BO_\s+"VFrameFormat"\s+ENUM\s+(.*);/.exec(line))) {
      for (const v of m[1].matchAll(/"([^"]*)"/g)) frameFormats.push(v[1]);
    } else if ((m = /^BA_\s+"VFrameFormat"\s+BO_\s+(\d+)\s+(\d+)\s*;/.exec(line))) {
      msgFormat.set(Number(m[1]), Number(m[2]));
    } else if (/^BA_\s+"ProtocolType"\s+"J1939"\s*;/.test(line)) {
      protocolJ1939 = true;
    }
  }

  const byId = new Map(), byPgn = new Map();
  for (const msg of messages) {
    const fmt = msgFormat.has(msg.dbcId) ? frameFormats[msgFormat.get(msg.dbcId)] : null;
    msg.j1939 = msg.ext && (fmt === 'J1939PG' || (fmt === null && protocolJ1939));
    const muxSig = msg.signals.find((s) => s.isMux);
    for (const s of msg.signals) {
      if (s.mux) s.mux.by = muxSig ? muxSig.name : null;
      if (msg.j1939 && !s.signed && s.type !== 'float' && s.len >= 2) s.na = 'j1939';
    }
    byId.set(`${msg.ext ? 'x' : 's'}${msg.id}`, msg);
    if (msg.j1939) {
      let pgn = (msg.id >>> 8) & 0x3ffff;
      if (((pgn >> 8) & 0xff) < 240) pgn &= 0x3ff00;
      msg.pgn = pgn;
      if (!byPgn.has(pgn)) byPgn.set(pgn, msg);
    }
  }
  return { messages, byId, byPgn, warnings, j1939: protocolJ1939 };
}

// ---------------------------------------------------------------- stored in the browser (IndexedDB)
const DB = 'tractorsim', STORE = 'dbc';

function openDb() {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === 'undefined') { reject(new Error('geen IndexedDB')); return; }
    const r = indexedDB.open(DB, 1);
    r.onupgradeneeded = () => r.result.createObjectStore(STORE, { keyPath: 'name' });
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

async function tx(mode, fn) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const t = db.transaction(STORE, mode);
    const req = fn(t.objectStore(STORE));
    t.oncomplete = () => { resolve(req?.result); db.close(); };
    t.onerror = () => { reject(t.error); db.close(); };
  });
}

/** [{ name, text, enabled }] — empty when the browser keeps nothing (private window). */
export async function loadStored() {
  try { return (await tx('readonly', (s) => s.getAll())) || []; } catch { return []; }
}
export async function saveStored(entry) { try { await tx('readwrite', (s) => s.put(entry)); } catch { /* not kept */ } }
export async function deleteStored(name) { try { await tx('readwrite', (s) => s.delete(name)); } catch { /* not kept */ } }
