// Task controller page: the implements (TC clients) with their DDOP and process data, the task, section control,
// the prescription map, the field and the autopilot, the map, and the TC messages.
import { h, hex, send, on, toast, isDemo, parseNum } from './util.js';
import { ddiInfo, ddiText } from './can/ddi.js';
import { MapView } from './tc/map.js';

const NONE = 0xffff;
const DET_TYPES = { 1: 'toestel', 2: 'functie', 3: 'bak / product', 4: 'sectie', 5: 'eenheid', 6: 'koppeling', 7: 'navigatiereferentie' };
let root, sideEl, mapView, mapCard, followBtn, treeEl, treeTabs, logEl, offEl;
let st = { enabled: false };
const pools = new Map(); // addr → { objects, byId }
const values = new Map(); // addr → Map(`${el}:${ddi}` → [actual, sent])
let shown = null; // address of the implement in the process data card
let logRows = [];
let logPaused = false;
let treeTimer = null;
const edits = new Set(); // inputs being edited: keep them while values update
let lastSig = '';
let moreOpen = false;

const fmt = (v, dec = 0) => (v === null || v === undefined || Number.isNaN(v) ? '—' : Number(v).toLocaleString('nl-BE', { minimumFractionDigits: dec, maximumFractionDigits: dec }));

export function mount(el) {
  root = el;
  sideEl = h('div', { class: 'tc-side' });
  const canvas = h('canvas', { class: 'tc-canvas', 'aria-label': 'Kaart van het perceel' });
  const tip = h('div', { class: 'tc-tip', hidden: true });
  const legend = h('div', { class: 'tc-legend' });
  mapView = new MapView(canvas, legend, tip);
  mapView.onFollow = (on) => followBtn.classList.toggle('on', on);
  followBtn = h('button', { class: 'seg on', onclick: () => { mapView.setFollow(!mapView.follow); followBtn.classList.toggle('on', mapView.follow); } }, 'Volgen');
  mapCard = h('section', { class: 'card tc-mapcard' },
    h('h3', null, 'Kaart', h('span', { class: 'tools' },
      h('span', { class: 'segs' },
        h('button', { class: 'seg', title: 'Inzoomen', onclick: () => zoom(1.5) }, '+'),
        h('button', { class: 'seg', title: 'Uitzoomen', onclick: () => zoom(1 / 1.5) }, '−'),
        h('button', { class: 'seg', title: 'Het hele perceel', onclick: () => { mapView.fitField(); followBtn.classList.remove('on'); } }, 'Perceel'),
        followBtn))),
    h('div', { class: 'tc-mapwrap' }, canvas, tip), legend);
  treeTabs = h('span', { class: 'tools' });
  treeEl = h('div', { class: 'tablewrap tc-tree' });
  logEl = h('div', { class: 'tablewrap log tc-log' });
  offEl = h('div', { class: 'note', hidden: true }, 'De task controller staat uit. Zet hem aan op de pagina Instellingen.');
  root.append(offEl, h('div', { class: 'tc-layout' }, sideEl,
    h('div', { class: 'tc-main' }, mapCard,
      h('section', { class: 'card' }, h('h3', null, 'Procesdata', treeTabs), treeEl),
      h('section', { class: 'card' }, h('h3', null, 'TC-berichten', h('span', { class: 'tools' },
        h('button', { class: 'btn small ghost', onclick: (e) => { logPaused = !logPaused; e.target.textContent = logPaused ? 'Verder' : 'Pauze'; } }, 'Pauze'),
        h('button', { class: 'btn small ghost', onclick: () => { logRows = []; renderLog(); } }, 'Wissen'))), logEl))));

  on('tc', (m) => { st = m; renderSide(); });
  on('tc.pool', (m) => {
    const byId = new Map(m.objects.map((o) => [o.id, o]));
    pools.set(m.addr, { objects: m.objects, byId });
    if (shown === null) shown = m.addr;
    renderTree();
  });
  on('tc.values', (m) => {
    for (const r of m.rows) {
      let vals = values.get(r.addr);
      if (!vals || r.reset) { vals = new Map(); values.set(r.addr, vals); }
      for (const [el, ddi, v, sent] of r.values) vals.set(`${el}:${ddi}`, [v, sent]);
    }
    scheduleTree();
  });
  on('tc.map', (m) => { if (m.full) mapView.setMap(m); else mapView.update(m); });
  on('tc.log', (m) => {
    if (logPaused && m.append) return;
    logRows = m.append ? logRows.concat(m.rows).slice(-400) : m.rows.slice(-400);
    renderLog();
  });
}

export function show() { mapView.draw(); }

function zoom(f) {
  const r = mapView.canvas.getBoundingClientRect();
  mapView.zoomAt(r.width / 2, r.height / 2, f);
}

// ---------------------------------------------------------------- side cards
function sw(checked, onchange, title) {
  return h('label', { class: 'switch', title }, h('input', { type: 'checkbox', checked, onchange }), h('i'));
}

function numIn(value, onchange, attrs = {}) {
  return h('input', { class: 'num', type: 'text', inputmode: 'decimal', value: String(value ?? ''), onchange: (e) => onchange(parseNum(e.target.value)), ...attrs });
}

function opts(values) { send({ t: 'tc.opts', values }); }

function renderSide() {
  offEl.hidden = st.enabled;
  if (!st.enabled) { sideEl.replaceChildren(); return; }
  // rebuild only when something else than the live values changed, and never under the operator's hands
  const sig = JSON.stringify({ ...st, task: { state: st.task?.state }, area: 0, overlapArea: 0, position: !!st.position, rate: null,
    states: null, decided: null, totals: null });
  const busy = document.activeElement && sideEl.contains(document.activeElement) && ['INPUT', 'SELECT'].includes(document.activeElement.tagName)
    && document.activeElement.type !== 'checkbox';
  if (sig === lastSig || busy) { renderLive(); return; }
  lastSig = sig;
  const o = st.opts || {};
  const t = st.task || {};
  const target = st.target;
  mapView.setUnit(target?.unit || st.map?.unit || '');
  const stateCls = { actief: 'ok', pauze: 'warn', uit: '' }[t.state] ?? '';
  const taskCard = h('section', { class: 'card' },
    h('h3', null, 'Taak', h('span', { class: `pill ${stateCls}` }, { actief: 'loopt', pauze: 'gepauzeerd', uit: 'gestopt' }[t.state] || t.state)),
    h('div', { class: 'toolbar' },
      h('button', { class: 'btn primary', disabled: t.state === 'actief', onclick: () => send({ t: 'tc.task', action: 'start' }) }, t.state === 'pauze' ? 'Verder' : 'Start'),
      h('button', { class: 'btn', disabled: t.state !== 'actief', onclick: () => send({ t: 'tc.task', action: 'pause' }) }, 'Pauze'),
      h('button', { class: 'btn', disabled: t.state === 'uit', onclick: () => send({ t: 'tc.task', action: 'stop' }) }, 'Stop'),
      h('button', { class: 'btn ghost', title: 'Dekking, spoor en logboek wissen', onclick: () => { if (confirm('Dekking, spoor en logboek van de taak wissen?')) send({ t: 'tc.task', action: 'reset' }); } }, 'Wissen')),
    h('dl', { class: 'tc-live' },
      h('dt', null, 'Task controller'), h('dd', null, st.address != null && st.address < 254 ? `adres ${hex(st.address)}, ${st.state}` : st.state, `, versie ${st.cfg?.version}`),
      h('dt', null, 'Positie'), h('dd', { id: 'tc-pos' }),
      h('dt', null, 'Duur'), h('dd', { id: 'tc-time' }),
      h('dt', null, 'Bewerkt'), h('dd', { id: 'tc-area' }),
      h('dt', null, 'Logboek'), h('dd', { id: 'tc-rows' }),
      h('dt', null, 'Totalen'), h('dd', { id: 'tc-totals' })),
    isDemo() ? null : h('div', { class: 'toolbar small' },
      h('a', { class: 'btn ghost small api-dl', href: '/api/tc/log.csv', download: '' }, 'Logboek (CSV)'),
      h('a', { class: 'btn ghost small api-dl', href: '/api/tc/dekking.geojson', download: '' }, 'Dekking (GeoJSON)')));

  const clients = h('section', { class: 'card' }, h('h3', null, 'Werktuigen'),
    st.clients?.length ? h('div', { class: 'tc-clients' }, st.clients.map((c) => h('div', { class: `tc-client${c.addr === st.implement ? ' sel' : ''}` },
      h('div', null, h('b', null, c.designator || 'zonder beschrijving'), ' ', h('span', { class: 'muted mono' }, hex(c.addr)),
        ' ', h('span', { class: `pill ${c.state === 'actief' ? 'ok' : c.state === 'fout' ? 'err' : 'warn'}` }, c.state)),
      h('div', { class: 'small muted' }, [c.version ? `TC-client versie ${c.version.version}` : null, c.sections ? `${c.sections} secties` : null,
        c.addr === st.implement ? 'gebruikt voor secties en taakkaart' : null].filter(Boolean).join(' · ')),
      c.progress ? h('progress', { max: c.progress.size, value: c.progress.got, class: 'tc-prog' }) : null,
      c.error ? h('div', { class: 'small err' }, c.error) : null,
      isDemo() || !c.active ? null : h('a', { class: 'small api-dl', href: `/api/tc/ddop/${c.addr}.bin`, download: '' }, 'DDOP downloaden'))))
      : h('p', { class: 'muted small' }, 'Nog geen werktuig aangemeld. Een TC-client (bv. TC_Client of het testwerktuig) meldt zich aan zodra hij de TC Status ziet.'));

  const secs = st.sections || [];
  const manual = o.scMode === 'handmatig';
  const scCard = h('section', { class: 'card' },
    h('h3', null, 'Sectiebesturing', h('span', { class: 'tools' }, sw(o.sc, (e) => opts({ sc: e.target.checked }), 'Sectiebesturing (TC-SC) aan/uit'))),
    secs.length ? [
      h('div', { class: 'row' }, h('span', { class: 'lbl' }, 'Modus'),
        h('span', { class: 'segs' }, [['auto', 'Automatisch'], ['handmatig', 'Met de hand']].map(([k, t2]) =>
          h('button', { class: `seg${o.scMode === k ? ' on' : ''}`, onclick: () => opts({ scMode: k }) }, t2)))),
      st.scAuto === false && o.sc ? h('p', { class: 'small warn-text' }, 'Het werktuig staat niet op automatische sectiebesturing (DDI 160): het volgt de TC niet.') : null,
      h('div', { class: 'tc-secs', style: { gridTemplateColumns: `repeat(${Math.min(secs.length, 16)}, minmax(0, 1fr))` } }, secs.map((s, i) => {
        const on = st.states?.[i];
        const want = st.decided?.[i];
        return h('button', {
          class: `tc-sec${on ? ' on' : ''}${want && !on ? ' want' : ''}`, disabled: !manual,
          title: `Sectie ${i + 1}: ${on ? 'aan' : 'uit'}${want !== undefined ? `, TC wil ${want ? 'aan' : 'uit'}` : ''} (breedte ${fmt(s.w, 2)} m)`,
          onclick: () => { const m = secs.map((_, k) => (o.manual?.[k] ?? true)); m[i] = !m[i]; opts({ manual: m }); },
        }, String(i + 1));
      })),
      h('details', { class: 'tc-more', open: moreOpen, ontoggle: (e) => { moreOpen = e.target.open; } }, h('summary', null, 'Instellingen'),
        row('Overlap', numIn(o.overlap, (v) => opts({ overlap: v })), '%', 'sectie uit als dit deel van de sectie op bewerkte grond komt'),
        row('Vooruitkijken aan', numIn(o.lookaheadOn, (v) => opts({ lookaheadOn: v })), 's'),
        row('Vooruitkijken uit', numIn(o.lookaheadOff, (v) => opts({ lookaheadOff: v })), 's'),
        row('Koppeling X', numIn(o.hitchX, (v) => opts({ hitchX: v })), 'm', 'van de GNSS-antenne tot de koppeling, negatief = achter de antenne'),
        row('Koppeling Y', numIn(o.hitchY, (v) => opts({ hitchY: v })), 'm', 'positief = rechts')),
    ] : h('p', { class: 'muted small' }, 'Het werktuig heeft geen secties in zijn beschrijving.'));

  const fileIn = h('input', { type: 'file', accept: '.geojson,.json,.zip', hidden: true, onchange: openMap });
  const lo = numIn(150, () => {}), hi = numIn(250, () => {});
  const geoCard = h('section', { class: 'card' },
    h('h3', null, 'Taakkaart', h('span', { class: 'tools' }, sw(o.geo, (e) => opts({ geo: e.target.checked }), 'Taakkaart (TC-GEO) aan/uit'))),
    st.map ? h('dl', null,
      h('dt', null, 'Kaart'), h('dd', null, `${st.map.name}, ${st.map.zones} zones`),
      h('dt', null, 'Dosis'), h('dd', null, `${fmt(st.map.min, 1)} – ${fmt(st.map.max, 1)} ${target?.unit || st.map.unit || ''}`),
      h('dt', null, 'Nu'), h('dd', { id: 'tc-rate' })) : h('p', { class: 'muted small' }, 'Geen taakkaart. Open een GeoJSON of een shapefile (.zip, WGS84), of maak een demokaart over het perceel.'),
    st.candidates?.length ? h('div', { class: 'row' }, h('span', { class: 'lbl' }, 'Stuurt'),
      h('select', { onchange: (e) => { const c = st.candidates[+e.target.value]; opts({ target: { element: c.element, ddi: c.ddi, scale: c.scale || ddiInfo(c.ddi).res, unit: c.unit || ddiInfo(c.ddi).unit } }); } },
        st.candidates.map((c, i) => h('option', { value: i, selected: target && c.element === target.element && c.ddi === target.ddi },
          `${c.designator} (el ${c.element}, DDI ${c.ddi}${c.unit ? `, ${c.unit}` : ''})`)))) : null,
    h('div', { class: 'toolbar small' },
      h('button', { class: 'btn', onclick: () => fileIn.click() }, 'Kaart openen…'), fileIn,
      st.map ? h('button', { class: 'btn ghost', onclick: () => send({ t: 'tc.clearMap' }) }, 'Wissen') : null),
    h('div', { class: 'toolbar small' }, h('span', { class: 'small' }, 'Demokaart van'), lo, h('span', { class: 'small' }, 'tot'), hi,
      h('button', { class: 'btn small', onclick: () => send({ t: 'tc.demoMap', low: parseNum(lo.value), high: parseNum(hi.value) }) }, 'Maken')));

  const ap = st.autopilot || {};
  const geoIn = h('input', { type: 'file', accept: '.geojson,.json', hidden: true, onchange: openField });
  const len = numIn(200, () => {}), wid = numIn(96, () => {});
  const fieldCard = h('section', { class: 'card' },
    h('h3', null, 'Perceel en rijden'),
    h('dl', null, h('dt', null, 'Perceel'), h('dd', null, st.fieldArea ? `${fmt(st.fieldArea / 10000, 2)} ha` : '—')),
    h('div', { class: 'toolbar small' }, h('span', { class: 'small' }, 'Rechthoek'), len, h('span', { class: 'small' }, '×'), wid, h('span', { class: 'small' }, 'm'),
      h('button', { class: 'btn small', title: 'Vanaf de huidige positie, in de rijrichting en naar rechts', onclick: () => send({ t: 'tc.field', length: parseNum(len.value), width: parseNum(wid.value) }) }, 'Perceel hier'),
      h('button', { class: 'btn small ghost', onclick: () => geoIn.click() }, 'Grens openen…'), geoIn),
    ap.possible ? [
      h('div', { class: 'row' }, h('span', { class: 'lbl' }, 'Automatisch rijden'), sw(ap.on, (e) => send({ t: 'tc.autopilot', on: e.target.checked })),
        h('span', { class: 'small muted' }, ap.on ? (ap.finished ? 'klaar: perceel gedaan' : `banen van ${fmt(ap.spacing, 1)} m`) : 'in banen over het perceel, met de snelheid van de tractor')),
      row('Baanafstand', numIn(o.spacing || 0, (v) => opts({ spacing: v })), 'm', '0 = de werkbreedte van het werktuig'),
    ] : h('p', { class: 'muted small' }, 'Automatisch rijden kan enkel met de eigen GNSS-ontvanger van de simulator.'));

  sideEl.replaceChildren(taskCard, clients, scCard, geoCard, fieldCard);
  renderLive();
  renderTabs();
}

function row(label, input, unit, hint) {
  return [h('div', { class: 'row norange' }, h('span', { class: 'lbl' }, label), h('span', { class: 'numwrap' }, input, h('span', { class: 'unit' }, unit))),
    hint ? h('div', { class: 'hint' }, hint) : null];
}

function renderLive() {
  const set = (id, text) => { const e = document.getElementById(id); if (e) e.textContent = text; };
  const t = st.task || {};
  const p = st.position;
  set('tc-pos', p ? `${p.source}, ${fmt(p.speed, 1)} km/h, koers ${fmt(p.heading, 0)}°` : 'geen positie');
  const s = Math.round(t.time || 0);
  set('tc-time', `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`);
  set('tc-area', `${fmt((st.area || 0) / 10000, 2)} ha${st.overlapArea ? `, waarvan ${fmt(st.overlapArea / 10000, 3)} ha dubbel` : ''}`);
  set('tc-rows', `${t.rows || 0} rijen (1 per seconde)`);
  set('tc-rate', st.rate === null || st.rate === undefined ? '—' : `${fmt(st.rate, 1)} ${st.target?.unit || ''}`);
  const tot = [];
  for (const [addr, list] of Object.entries(st.totals || {})) {
    const pool = pools.get(Number(addr));
    for (const x of list) {
      const det = pool && pool.objects.find((q) => q.table === 'DET' && q.number === x.element);
      const o = det && det.children.map((c) => pool.byId.get(c)).find((q) => q && q.table === 'DPD' && q.ddi === x.ddi);
      const def = o ? defOf(pool, o) : { info: ddiInfo(x.ddi), dvp: null };
      tot.push(`${x.designator}: ${showValue(def, x.value)}${x.task !== null && x.task !== undefined ? ` (taak ${showValue(def, x.task)})` : ''}`);
    }
  }
  set('tc-totals', tot.length ? tot.join(' · ') : '—');
  sideEl.querySelectorAll('.tc-sec').forEach((b, i) => {
    const on = !!st.states?.[i], want = !!st.decided?.[i];
    b.classList.toggle('on', on);
    b.classList.toggle('want', want && !on);
  });
}

async function openMap(e) {
  const f = e.target.files[0];
  e.target.value = '';
  if (!f) return;
  if (f.size > 8 * 1024 * 1024) { toast('Kaart te groot (max. 8 MB)', true); return; }
  const data = await f.arrayBuffer();
  send({ t: 'tc.map', name: f.name, data: b64(data) });
}

async function openField(e) {
  const f = e.target.files[0];
  e.target.value = '';
  if (f) send({ t: 'tc.field', geojson: await f.text() });
}

function b64(buf) {
  const b = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode(...b.subarray(i, i + 0x8000));
  return btoa(s);
}

// ---------------------------------------------------------------- process data tree
function renderTabs() {
  const addrs = (st.clients || []).filter((c) => pools.has(c.addr)).map((c) => c.addr);
  if (shown === null || !addrs.includes(shown)) shown = addrs.includes(st.implement) ? st.implement : addrs[0] ?? null;
  treeTabs.replaceChildren(addrs.length > 1 ? h('span', { class: 'segs' }, addrs.map((a) =>
    h('button', { class: `seg${a === shown ? ' on' : ''}`, onclick: () => { shown = a; renderTabs(); renderTree(); } }, hex(a)))) : '');
  renderTree();
}

function scheduleTree() {
  if (treeTimer) return;
  treeTimer = setTimeout(() => { treeTimer = null; updateValues(); }, 250);
}

function showValue(def, raw) {
  if (raw === null || raw === undefined) return '—';
  const txt = ddiText(def.info, raw);
  if (txt !== null) return txt;
  if (def.dvp) return `${fmt((raw + def.dvp.offset) * def.dvp.scale, def.dvp.decimals)} ${def.dvp.unit}`.trim();
  const res = def.info.res ?? 1;
  return `${fmt(raw * res, res < 1 ? Math.min(6, Math.ceil(-Math.log10(res) - 1e-9)) : 0)} ${def.info.unit}`.trim();
}

function defOf(pool, o) {
  const dvp = o.presentation !== NONE ? pool.byId.get(o.presentation) : null;
  return { info: ddiInfo(o.ddi), dvp: dvp && dvp.table === 'DVP' ? dvp : null };
}

function toRaw(def, text) {
  const v = parseNum(text);
  if (Number.isNaN(v)) return null;
  if (def.dvp) return Math.round(v / def.dvp.scale - def.dvp.offset);
  return Math.round(v / (def.info.res ?? 1));
}

function renderTree() {
  const pool = pools.get(shown);
  if (!pool) { treeEl.replaceChildren(h('p', { class: 'muted small' }, 'Nog geen beschrijving (DDOP) ontvangen.')); return; }
  const dets = pool.objects.filter((o) => o.table === 'DET');
  const numberOf = new Map(dets.map((d) => [d.id, d.number]));
  const depth = (d) => { let n = 0, p = pool.byId.get(d.parent); while (p && p.table === 'DET' && n < 20) { n++; p = pool.byId.get(p.parent); } return n; };
  const rows = [];
  const dev = pool.objects.find((o) => o.table === 'DVC');
  if (dev) rows.push(h('tr', { class: 'tc-dev' }, h('td', { colspan: 5 }, h('b', null, dev.designator), ` · software ${dev.softwareVersion} · serienr. ${dev.serial} · label `,
    h('span', { class: 'mono' }, String.fromCharCode(...dev.structureLabel.map((c) => (c >= 32 && c < 127 ? c : 46)))))));
  const ordered = [];
  const visit = (parentId) => {
    for (const d of dets.filter((x) => x.parent === parentId)) { ordered.push(d); visit(d.id); }
  };
  visit(dev ? dev.id : -1);
  for (const d of dets) if (!ordered.includes(d)) ordered.push(d);
  for (const d of ordered) {
    rows.push(h('tr', { class: 'tc-el' }, h('td', { colspan: 5, style: { paddingLeft: `${8 + depth(d) * 16}px` } },
      h('b', null, d.designator), h('span', { class: 'muted' }, ` · ${DET_TYPES[d.type] || `type ${d.type}`} · element ${d.number}`))));
    for (const cid of d.children) {
      const o = pool.byId.get(cid);
      if (!o) continue;
      const def = defOf(pool, o);
      const key = `${numberOf.get(d.id)}:${o.ddi}`;
      const pad = { paddingLeft: `${24 + depth(d) * 16}px` };
      if (o.table === 'DPT') {
        rows.push(h('tr', null, h('td', { style: pad }, o.designator || def.info.name), h('td', { class: 'muted small' }, `DDI ${o.ddi}`),
          h('td', { class: 'r' }, showValue(def, o.value)), h('td', { class: 'muted small' }, 'eigenschap'), h('td')));
        continue;
      }
      if (o.table !== 'DPD' || o.ddi === 0xdfff) continue;
      const settable = !!(o.props & 2);
      let edit = null;
      if (settable) {
        const inp = h('input', { class: 'iv', type: 'text', inputmode: 'decimal', placeholder: def.dvp?.unit || def.info.unit || '',
          onfocus: () => edits.add(key), onblur: () => edits.delete(key),
          onkeydown: (e) => { if (e.key === 'Enter') go(); } });
        const go = () => {
          const raw = toRaw(def, inp.value);
          if (raw === null) { toast('Geen getal', true); return; }
          send({ t: 'tc.value', addr: shown, element: numberOf.get(d.id), ddi: o.ddi, value: raw });
          inp.value = '';
        };
        edit = h('span', { class: 'tc-edit' }, inp, h('button', { class: 'btn small', onclick: go }, 'Stuur'));
      }
      const trig = [o.triggers & 1 && 'tijd', o.triggers & 2 && 'afstand', o.triggers & 4 && 'drempel', o.triggers & 8 && 'verandering', o.triggers & 16 && 'totaal'].filter(Boolean);
      rows.push(h('tr', { 'data-key': key },
        h('td', { style: pad, title: def.info.name }, o.designator || def.info.name),
        h('td', { class: 'muted small', title: def.info.name }, `DDI ${o.ddi}`),
        h('td', { class: 'r tc-val' }, '—'),
        h('td', { class: 'small tc-sent' }, ''),
        h('td', { class: 'small' }, edit || h('span', { class: 'muted' }, trig.join(', ')))));
    }
  }
  treeEl.replaceChildren(h('table', { class: 'tbl' },
    h('thead', null, h('tr', null, h('th', null, 'Naam'), h('th', null, 'DDI'), h('th', { class: 'r' }, 'Waarde werktuig'), h('th', null, 'Gestuurd door de TC'), h('th', null, 'Instellen / meten'))),
    h('tbody', null, rows)));
  updateValues();
}

function updateValues() {
  const pool = pools.get(shown);
  const vals = values.get(shown);
  if (!pool || !vals) return;
  const numberOf = new Map(pool.objects.filter((o) => o.table === 'DET').map((d) => [d.id, d.number]));
  for (const tr of treeEl.querySelectorAll('tr[data-key]')) {
    const key = tr.dataset.key;
    const v = vals.get(key);
    if (!v) continue;
    const [el, ddi] = key.split(':').map(Number);
    const det = pool.objects.find((o) => o.table === 'DET' && numberOf.get(o.id) === el);
    const o = det && det.children.map((c) => pool.byId.get(c)).find((x) => x && x.ddi === ddi);
    if (!o) continue;
    const def = defOf(pool, o);
    tr.querySelector('.tc-val').textContent = showValue(def, v[0]);
    tr.querySelector('.tc-sent').textContent = v[1] === null || v[1] === undefined ? '' : showValue(def, v[1]);
  }
}

// ---------------------------------------------------------------- log
function renderLog() {
  const rows = logRows.slice(-200).reverse().map((r) => h('tr', null,
    h('td', { class: 'mono small' }, new Date(r.t * 1000).toLocaleTimeString('nl-BE')),
    h('td', null, h('span', { class: `pill ${r.dir === 'in' ? '' : 'ok'}` }, r.dir === 'in' ? '→ TC' : 'TC →')),
    h('td', { class: 'mono small' }, r.addr === 255 ? 'alle' : hex(r.addr)),
    h('td', null, r.func), h('td', { class: 'mono small' }, r.text)));
  logEl.replaceChildren(h('table', { class: 'tbl' }, h('tbody', null, rows)));
}
