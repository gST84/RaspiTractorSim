// Map of the task controller page on a canvas: the prescription zones, the field boundary, the coverage, the track
// and the tractor with its sections. Coordinates come from the server in metres (e = east, n = north of the field
// origin); the view is a centre and a scale (pixels per metre), north up.
//
// Colours: the zones use one hue from light to dark (low → high rate, 5 classes, with a legend); the coverage is the
// accent colour, twice-worked ground the warning colour; sections are accent (on) or muted (off).

/** Sequential blue ramp (5 ordinal classes): light mode light → dark, dark mode dark → light. */
export const RAMP = {
  light: ['#86b6ef', '#5598e7', '#2a78d6', '#1c5cab', '#104281'],
  dark: ['#184f95', '#256abf', '#3987e5', '#6da7ec', '#9ec5f4'],
};

/** A round step near x/n (1, 2, 2.5 or 5 × 10^k). */
export function niceStep(range, n) {
  if (!(range > 0)) return 1;
  const raw = range / n;
  const p = 10 ** Math.floor(Math.log10(raw));
  const m = raw / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
}

/** Up to `n` classes with round limits that cover [min, max]: [{ lo, hi }]. One class when all rates are equal. */
export function classes(min, max, n = 5) {
  if (!(max > min)) return [{ lo: min, hi: max }];
  const step = niceStep(max - min, n);
  const lo0 = Math.floor(min / step) * step;
  const out = [];
  for (let lo = lo0; lo < max - 1e-9 && out.length < n + 1; lo += step) out.push({ lo: +lo.toPrecision(12), hi: +(lo + step).toPrecision(12) });
  return out;
}

export function classOf(v, cls) {
  for (let i = 0; i < cls.length; i++) if (v < cls[i].hi || i === cls.length - 1) return i;
  return cls.length - 1;
}

/** Point in polygon with holes (first ring outside, the others holes); points [[e, n], …]. */
export function insideRings(e, n, rings) {
  const inRing = (r) => {
    let hit = false;
    for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
      const [xi, yi] = r[i], [xj, yj] = r[j];
      if ((yi > n) !== (yj > n) && e < ((xj - xi) * (n - yi)) / (yj - yi) + xi) hit = !hit;
    }
    return hit;
  };
  return rings.length > 0 && inRing(rings[0]) && !rings.slice(1).some(inRing);
}

/** View that shows the box [x0, y0, x1, y1] (metres) in w × h pixels with a margin. */
export function fit(box, w, h, margin = 0.08) {
  const [x0, y0, x1, y1] = box;
  const bw = Math.max(1, x1 - x0), bh = Math.max(1, y1 - y0);
  const s = Math.min(w / (bw * (1 + 2 * margin)), h / (bh * (1 + 2 * margin)));
  return { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, s };
}

export const project = (v, w, h, e, n) => [w / 2 + (e - v.cx) * v.s, h / 2 - (n - v.cy) * v.s];
export const unproject = (v, w, h, x, y) => [v.cx + (x - w / 2) / v.s, v.cy - (y - h / 2) / v.s];

/** Distance for the scale bar: a round number of metres that is 60-140 px long. */
export function scaleBar(s) {
  const m = niceStep(100 / s, 1);
  return { m, px: m * s };
}

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const rgb = (c) => {
  const m = /^#?([0-9a-f]{6})$/i.exec(c);
  if (!m) return [128, 128, 128];
  const v = parseInt(m[1], 16);
  return [v >> 16, (v >> 8) & 255, v & 255];
};
const dark = () => typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: dark)').matches;

export class MapView {
  constructor(canvas, legend, tip) {
    this.canvas = canvas;
    this.legend = legend;
    this.tip = tip;
    this.view = null;
    this.follow = true;
    this.field = null;
    this.presc = null;
    this.cls = [];
    this.cov = null; // { x0, y0, w, h, cell, block, cells: Uint8Array, img: canvas }
    this.track = [];
    this.pose = null;
    this.unit = '';
    this.pending = false;
    this.drag = null;
    this.pointers = new Map();
    canvas.addEventListener('wheel', (e) => { e.preventDefault(); this.zoomAt(e.offsetX, e.offsetY, e.deltaY < 0 ? 1.25 : 0.8); }, { passive: false });
    canvas.addEventListener('pointerdown', (e) => this.down(e));
    canvas.addEventListener('pointermove', (e) => this.move(e));
    canvas.addEventListener('pointerup', (e) => this.up(e));
    canvas.addEventListener('pointercancel', (e) => this.up(e));
    canvas.addEventListener('pointerleave', () => { if (!this.drag) this.tip.hidden = true; });
    if (typeof ResizeObserver === 'function') new ResizeObserver(() => this.draw()).observe(canvas);
    if (typeof matchMedia === 'function') matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { this.paintCoverage(); this.renderLegend(); this.draw(); });
  }

  // ---------------------------------------------------------------- data
  setMap(m) {
    this.field = m.field;
    this.setPresc(m.presc);
    this.track = (m.track || []).slice();
    if (m.coverage) {
      const c = m.coverage;
      this.cov = { ...c, cells: new Uint8Array(c.w * c.h), img: document.createElement('canvas') };
      this.cov.img.width = c.w;
      this.cov.img.height = c.h;
      for (const b of m.blocks || []) this.block(b, false);
      this.paintCoverage();
    } else this.cov = null;
    if (m.pose !== undefined) this.pose = m.pose;
    if (!this.view && this.field) this.fitField();
    this.renderLegend();
    this.draw();
  }

  update(u) {
    if (u.full) { this.setMap(u); return; }
    if (u.track?.length) {
      this.track.push(...u.track);
      if (this.track.length > 40000) this.track.splice(0, 10000);
    }
    for (const b of u.blocks || []) this.block(b, true);
    this.pose = u.pose ?? this.pose;
    this.draw();
  }

  setPresc(p) {
    this.presc = p && p.zones?.length ? p : null;
    if (this.presc) {
      const rates = this.presc.zones.map((z) => z.rate);
      this.cls = classes(Math.min(...rates), Math.max(...rates));
      for (const z of this.presc.zones) {
        const xs = z.rings[0].map((q) => q[0]), ys = z.rings[0].map((q) => q[1]);
        z.box = [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
      }
    } else this.cls = [];
  }

  setUnit(u) {
    if (u !== this.unit) { this.unit = u || ''; this.renderLegend(); }
  }

  block([bi, bj, b64], paint) {
    const c = this.cov;
    if (!c) return;
    const raw = atob(b64);
    const B = c.block;
    for (let y = 0; y < B; y++) {
      const j = bj * B + y;
      if (j >= c.h) break;
      for (let x = 0; x < B; x++) {
        const i = bi * B + x;
        if (i < c.w) c.cells[j * c.w + i] = raw.charCodeAt(y * B + x);
      }
    }
    if (paint) this.paintBlock(bi, bj);
  }

  coverageColours() {
    const [ar, ag, ab] = rgb(css('--accent'));
    const [wr, wg, wb] = rgb(css('--warn'));
    return [null, [ar, ag, ab, 165], [wr, wg, wb, 215]];
  }

  paintBlock(bi, bj, colours = this.coverageColours()) {
    const c = this.cov;
    const B = c.block;
    const ctx = c.img.getContext('2d');
    const img = ctx.createImageData(B, B);
    for (let y = 0; y < B; y++) {
      const j = bj * B + y;
      if (j >= c.h) break;
      for (let x = 0; x < B; x++) {
        const i = bi * B + x;
        if (i >= c.w) break;
        const col = colours[c.cells[j * c.w + i]];
        if (!col) continue;
        const k = ((B - 1 - y) * B + x) * 4; // image rows go down, cells go north
        img.data[k] = col[0]; img.data[k + 1] = col[1]; img.data[k + 2] = col[2]; img.data[k + 3] = col[3];
      }
    }
    ctx.putImageData(img, bi * B, c.h - (bj + 1) * B);
  }

  paintCoverage() {
    const c = this.cov;
    if (!c) return;
    c.img.getContext('2d').clearRect(0, 0, c.w, c.h);
    const colours = this.coverageColours();
    for (let bj = 0; bj * c.block < c.h; bj++) {
      for (let bi = 0; bi * c.block < c.w; bi++) {
        let any = false;
        for (let y = 0; y < c.block && !any; y++) {
          const j = bj * c.block + y;
          if (j >= c.h) break;
          for (let x = 0; x < c.block; x++) if (c.cells[j * c.w + bi * c.block + x] && bi * c.block + x < c.w) { any = true; break; }
        }
        if (any) this.paintBlock(bi, bj, colours);
      }
    }
  }

  // ---------------------------------------------------------------- view
  size() {
    const r = this.canvas.getBoundingClientRect();
    return [Math.max(10, r.width), Math.max(10, r.height)];
  }

  fitField() {
    if (!this.field) return;
    const r = this.canvas.getBoundingClientRect();
    if (r.width < 50 || r.height < 50) { this.needFit = true; return; } // hidden page: fit once it is shown
    this.needFit = false;
    const xs = this.field.ring.map((p) => p[0]), ys = this.field.ring.map((p) => p[1]);
    const [w, h] = this.size();
    this.view = fit([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)], w, h);
    this.follow = false;
    this.onFollow?.(false);
    this.draw();
  }

  zoomAt(x, y, f) {
    if (!this.view) return;
    const [w, h] = this.size();
    const [e, n] = unproject(this.view, w, h, x, y);
    this.view.s = Math.min(200, Math.max(0.02, this.view.s * f));
    if (!this.follow) {
      const [x2, y2] = project(this.view, w, h, e, n);
      this.view.cx += (x2 - x) / this.view.s;
      this.view.cy -= (y2 - y) / this.view.s;
    }
    this.draw();
  }

  down(e) {
    this.canvas.setPointerCapture(e.pointerId);
    this.pointers.set(e.pointerId, [e.offsetX, e.offsetY]);
    this.drag = { x: e.offsetX, y: e.offsetY, moved: false, dist: this.pinchDist() };
  }

  pinchDist() {
    if (this.pointers.size < 2) return null;
    const [a, b] = [...this.pointers.values()];
    return Math.hypot(a[0] - b[0], a[1] - b[1]);
  }

  move(e) {
    if (this.pointers.has(e.pointerId)) this.pointers.set(e.pointerId, [e.offsetX, e.offsetY]);
    if (this.drag && this.view) {
      const d = this.pinchDist();
      if (d && this.drag.dist) {
        this.zoomAt(e.offsetX, e.offsetY, d / this.drag.dist);
        this.drag.dist = d;
        return;
      }
      const dx = e.offsetX - this.drag.x, dy = e.offsetY - this.drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) this.drag.moved = true;
      if (this.drag.moved) {
        this.follow = false;
        this.view.cx -= dx / this.view.s;
        this.view.cy += dy / this.view.s;
        this.drag.x = e.offsetX;
        this.drag.y = e.offsetY;
        this.onFollow?.(false);
        this.draw();
      }
      return;
    }
    this.hover(e.offsetX, e.offsetY);
  }

  up(e) {
    this.pointers.delete(e.pointerId);
    if (this.pointers.size === 0) this.drag = null;
  }

  setFollow(on) {
    this.follow = on;
    this.draw();
  }

  // ---------------------------------------------------------------- tooltip
  rateAt(e, n) {
    if (!this.presc) return null;
    for (const z of this.presc.zones) {
      const [x0, y0, x1, y1] = z.box;
      if (e >= x0 && e <= x1 && n >= y0 && n <= y1 && insideRings(e, n, z.rings)) return z.rate;
    }
    return this.presc.default ?? null;
  }

  coverAt(e, n) {
    const c = this.cov;
    if (!c) return 0;
    const i = Math.floor((e - c.x0) / c.cell), j = Math.floor((n - c.y0) / c.cell);
    return i >= 0 && j >= 0 && i < c.w && j < c.h ? c.cells[j * c.w + i] : 0;
  }

  hover(x, y) {
    if (!this.view) return;
    const [w, h] = this.size();
    const [e, n] = unproject(this.view, w, h, x, y);
    const rate = this.rateAt(e, n);
    const cov = this.coverAt(e, n);
    const lines = [];
    if (rate !== null) lines.push(`Dosis ${fmtNum(rate)} ${this.unit}`.trim());
    lines.push(cov === 0 ? 'niet bewerkt' : cov === 1 ? 'bewerkt' : 'dubbel bewerkt');
    if (this.field && !insideRings(e, n, [this.field.ring])) lines.push('buiten het perceel');
    this.tip.textContent = lines.join(' · ');
    this.tip.hidden = false;
    const left = Math.min(x + 14, w - this.tip.offsetWidth - 4);
    this.tip.style.left = `${Math.max(4, left)}px`;
    this.tip.style.top = `${y + 16 > h - 30 ? y - 30 : y + 16}px`;
  }

  // ---------------------------------------------------------------- legend
  renderLegend() {
    if (!this.legend) return;
    const ramp = dark() ? RAMP.dark : RAMP.light;
    const items = [];
    const sw = (style, text) => {
      const s = document.createElement('span');
      s.className = 'tcl-item';
      const i = document.createElement('i');
      Object.assign(i.style, style);
      s.append(i, document.createTextNode(text));
      return s;
    };
    if (this.presc) {
      const t = document.createElement('span');
      t.className = 'tcl-title';
      t.textContent = `Taakkaart${this.unit ? ` (${this.unit})` : ''}:`;
      items.push(t);
      this.cls.forEach((c, i) => items.push(sw({ background: ramp[this.rampIndex(i)] }, c.lo === c.hi ? fmtNum(c.lo) : `${fmtNum(c.lo)}–${fmtNum(c.hi)}`)));
    }
    items.push(sw({ background: 'color-mix(in srgb, var(--accent) 65%, transparent)' }, 'bewerkt'));
    items.push(sw({ background: 'color-mix(in srgb, var(--warn) 85%, transparent)' }, 'dubbel bewerkt'));
    items.push(sw({ background: 'var(--accent)', height: '4px' }, 'sectie aan'));
    items.push(sw({ background: 'var(--muted)', height: '4px' }, 'sectie uit'));
    this.legend.replaceChildren(...items);
  }

  rampIndex(i) {
    const n = this.cls.length;
    return n <= 1 ? 2 : Math.round((i / (n - 1)) * 4);
  }

  // ---------------------------------------------------------------- drawing
  draw() {
    if (this.pending) return;
    this.pending = true;
    requestAnimationFrame(() => { this.pending = false; this.render(); });
  }

  render() {
    const cv = this.canvas;
    const rect = cv.getBoundingClientRect();
    if (rect.width < 50 || rect.height < 50) return; // hidden
    if (this.needFit && this.field) this.fitField();
    const [w, h] = this.size();
    const dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
    }
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = css('--panel2');
    ctx.fillRect(0, 0, w, h);
    if (!this.view) {
      if (this.field) this.fitField();
      else if (this.pose) this.view = { cx: this.pose.e, cy: this.pose.n, s: 2 };
      else return;
    }
    if (this.follow && this.pose) { this.view.cx = this.pose.e; this.view.cy = this.pose.n; }
    const v = this.view;
    const P = (e, n) => project(v, w, h, e, n);
    const ink = css('--ink'), muted = css('--muted'), accent = css('--accent'), panel = css('--panel');
    const path = (ring) => {
      ring.forEach(([e, n], i) => { const [x, y] = P(e, n); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
      ctx.closePath();
    };
    // zones
    if (this.presc) {
      const ramp = dark() ? RAMP.dark : RAMP.light;
      ctx.save();
      if (this.field) { // only what lies in the field
        ctx.beginPath();
        path(this.field.ring);
        ctx.clip();
      }
      for (const z of this.presc.zones) {
        ctx.beginPath();
        z.rings.forEach(path);
        ctx.fillStyle = ramp[this.rampIndex(classOf(z.rate, this.cls))];
        ctx.fill('evenodd');
      }
      ctx.restore();
    }
    // coverage
    const c = this.cov;
    if (c) {
      const [x0, y1] = P(c.x0, c.y0 + c.h * c.cell);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(c.img, x0, y1, c.w * c.cell * v.s, c.h * c.cell * v.s);
    }
    // field boundary
    if (this.field) {
      ctx.beginPath();
      path(this.field.ring);
      ctx.lineWidth = 2;
      ctx.strokeStyle = ink;
      ctx.stroke();
    }
    // track
    if (this.track.length > 1) {
      ctx.beginPath();
      this.track.forEach(([e, n], i) => { const [x, y] = P(e, n); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
      ctx.lineWidth = 1;
      ctx.strokeStyle = muted;
      ctx.globalAlpha = 0.7;
      ctx.stroke();
      ctx.globalAlpha = 1;
    }
    // implement and tractor
    const p = this.pose;
    if (p) {
      const [ax, ay] = P(p.e, p.n);
      if (p.sections?.length) {
        const mid = p.sections[Math.floor(p.sections.length / 2)];
        const [mx, my] = P((mid[0] + mid[2]) / 2, (mid[1] + mid[3]) / 2);
        ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(mx, my);
        ctx.lineWidth = 2; ctx.strokeStyle = ink; ctx.stroke();
        for (const [e0, n0, e1, n1, on] of p.sections) {
          const [x0, y0] = P(e0, n0), [x1, y1] = P(e1, n1);
          const gap = Math.min(1.5, Math.hypot(x1 - x0, y1 - y0) * 0.08); // 1-2 px between sections
          const ux = (x1 - x0) / (Math.hypot(x1 - x0, y1 - y0) || 1), uy = (y1 - y0) / (Math.hypot(x1 - x0, y1 - y0) || 1);
          ctx.beginPath(); ctx.moveTo(x0 + ux * gap, y0 + uy * gap); ctx.lineTo(x1 - ux * gap, y1 - uy * gap);
          ctx.lineCap = 'round';
          ctx.lineWidth = 8; ctx.strokeStyle = panel; ctx.stroke(); // surface ring
          ctx.lineWidth = 5; ctx.strokeStyle = on ? accent : muted; ctx.stroke();
        }
        ctx.lineCap = 'butt';
      }
      const hd = (p.heading * Math.PI) / 180;
      const fx = Math.sin(hd), fy = -Math.cos(hd); // screen: y down
      const L = 11, W = 7;
      ctx.beginPath();
      ctx.moveTo(ax + fx * L, ay + fy * L);
      ctx.lineTo(ax - fx * L * 0.6 - fy * W, ay - fy * L * 0.6 + fx * W);
      ctx.lineTo(ax - fx * L * 0.6 + fy * W, ay - fy * L * 0.6 - fx * W);
      ctx.closePath();
      ctx.fillStyle = ink; ctx.fill();
      ctx.lineWidth = 2; ctx.strokeStyle = panel; ctx.stroke();
    }
    // scale bar
    const sb = scaleBar(v.s);
    ctx.fillStyle = ink; ctx.strokeStyle = ink; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(12, h - 14); ctx.lineTo(12 + sb.px, h - 14); ctx.moveTo(12, h - 18); ctx.lineTo(12, h - 10); ctx.moveTo(12 + sb.px, h - 18); ctx.lineTo(12 + sb.px, h - 10); ctx.stroke();
    ctx.font = '11px system-ui, sans-serif';
    ctx.fillText(`${fmtNum(sb.m)} m`, 16 + sb.px, h - 10);
    // north arrow
    ctx.fillStyle = muted;
    ctx.fillText('N ↑', w - 30, 16);
  }
}

function fmtNum(v) {
  return Number(v).toLocaleString('nl-BE', { maximumFractionDigits: Math.abs(v) < 10 ? 2 : Math.abs(v) < 100 ? 1 : 0 });
}
