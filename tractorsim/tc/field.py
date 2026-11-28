"""Field geometry for the task controller: a local plane in metres, the field boundary, the coverage raster and the
autopilot that drives the simulated tractor over the field in lanes.

Local coordinates: e = metres east, n = metres north of the origin (an equirectangular projection, accurate to a few
centimetres over a few kilometres). Headings are compass degrees: 0 = north, 90 = east.
"""
import base64
import math

R_EARTH = 6378137.0


class Frame:
    """Projection between latitude/longitude and the local plane around an origin."""

    def __init__(self, lat0, lon0):
        self.lat0, self.lon0 = float(lat0), float(lon0)
        self.ky = math.pi / 180.0 * R_EARTH
        self.kx = self.ky * math.cos(math.radians(self.lat0))

    def to_local(self, lat, lon):
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky

    def to_geo(self, e, n):
        return self.lat0 + n / self.ky, self.lon0 + e / self.kx


def forward(heading):
    h = math.radians(heading)
    return math.sin(h), math.cos(h)


def right(heading):
    h = math.radians(heading)
    return math.cos(h), -math.sin(h)


def inside(pt, ring):
    """Point in polygon (even-odd rule); ring = [(e, n), …]."""
    x, y = pt
    hit = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def inside_polygon(pt, rings):
    """Polygon with holes: inside the first ring and outside the others."""
    return bool(rings) and inside(pt, rings[0]) and not any(inside(pt, r) for r in rings[1:])


def ring_area(ring):
    a = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i - 1]
        x2, y2 = ring[i]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


class Field:
    """Field boundary in local coordinates. A rectangle also has its lane direction (the long side) for the autopilot."""

    def __init__(self, frame, ring, name="Perceel", rect=None):
        self.frame = frame
        self.ring = [tuple(p) for p in ring]
        self.name = name
        self.rect = rect  # {"heading", "length", "width"} when made with rectangle(); origin = (0, 0)

    @classmethod
    def rectangle(cls, lat, lon, heading, length, width, name="Perceel"):
        """Rectangle that starts at the position (lat, lon): `length` m in the direction `heading`, `width` m to the right."""
        f = Frame(lat, lon)
        fx, fy = forward(heading)
        rx, ry = right(heading)
        ring = [(0.0, 0.0), (fx * length, fy * length), (fx * length + rx * width, fy * length + ry * width), (rx * width, ry * width)]
        return cls(f, ring, name, {"heading": heading % 360.0, "length": float(length), "width": float(width)})

    @property
    def area(self):
        return ring_area(self.ring)

    def contains(self, e, n):
        return inside((e, n), self.ring)

    def bbox(self):
        xs = [p[0] for p in self.ring]
        ys = [p[1] for p in self.ring]
        return min(xs), min(ys), max(xs), max(ys)

    def to_json(self):
        return {"name": self.name, "origin": [self.frame.lat0, self.frame.lon0], "ring": [list(p) for p in self.ring],
                "rect": self.rect, "area": round(self.area, 1)}

    @classmethod
    def from_json(cls, d):
        return cls(Frame(*d["origin"]), d["ring"], d.get("name", "Perceel"), d.get("rect"))

    def geo_ring(self):
        return [self.frame.to_geo(e, n) for e, n in self.ring]


class Coverage:
    """Raster of the worked area: per cell the number of times it was worked (0, 1, 2 = 2 or more). Cells change in
    blocks of BLOCK × BLOCK; `take_dirty` gives the changed blocks for the web page."""

    BLOCK = 32

    def __init__(self, x0, y0, x1, y1, cell=0.5):
        self.cell = float(cell)
        self.x0, self.y0 = math.floor(x0), math.floor(y0)
        self.w = max(1, int(math.ceil((x1 - self.x0) / self.cell)))
        self.h = max(1, int(math.ceil((y1 - self.y0) / self.cell)))
        while self.w * self.h > 4_000_000:  # keep memory and the page reasonable for big fields
            self.cell *= 2
            self.w = max(1, int(math.ceil((x1 - self.x0) / self.cell)))
            self.h = max(1, int(math.ceil((y1 - self.y0) / self.cell)))
        self.cells = bytearray(self.w * self.h)
        self.dirty = set()
        self.worked = 0  # cells worked at least once
        self.double = 0  # cells worked twice or more

    @classmethod
    def for_field(cls, field, margin=30.0, cell=0.5):
        x0, y0, x1, y1 = field.bbox()
        return cls(x0 - margin, y0 - margin, x1 + margin, y1 + margin, cell)

    def _idx(self, e, n):
        i = int((e - self.x0) / self.cell)
        j = int((n - self.y0) / self.cell)
        if 0 <= i < self.w and 0 <= j < self.h:
            return j * self.w + i
        return -1

    def count(self, e, n):
        k = self._idx(e, n)
        return self.cells[k] if k >= 0 else 0

    def paint(self, quad):
        """Marks the cells whose centre lies in the convex quadrilateral `quad` (4 points, in order). → new cells."""
        xs = [p[0] for p in quad]
        ys = [p[1] for p in quad]
        i0 = max(0, int((min(xs) - self.x0) / self.cell))
        i1 = min(self.w - 1, int((max(xs) - self.x0) / self.cell))
        j0 = max(0, int((min(ys) - self.y0) / self.cell))
        j1 = min(self.h - 1, int((max(ys) - self.y0) / self.cell))
        if i0 > i1 or j0 > j1:
            return 0
        edges = []
        for k in range(4):
            ax, ay = quad[k]
            bx, by = quad[(k + 1) % 4]
            edges.append((ax, ay, bx - ax, by - ay))
        # orientation of the quad (shoelace): counter-clockwise → the inside is left of every edge
        area2 = sum(quad[k][0] * quad[(k + 1) % 4][1] - quad[(k + 1) % 4][0] * quad[k][1] for k in range(4))
        if abs(area2) < 1e-6:
            return 0
        sign = 1.0 if area2 > 0 else -1.0
        new = 0
        cells, w, c = self.cells, self.w, self.cell
        for j in range(j0, j1 + 1):
            y = self.y0 + (j + 0.5) * c
            row = j * w
            for i in range(i0, i1 + 1):
                x = self.x0 + (i + 0.5) * c
                ok = True
                for ax, ay, ex, ey in edges:
                    if sign * (ex * (y - ay) - ey * (x - ax)) < -1e-9:
                        ok = False
                        break
                if not ok:
                    continue
                k = row + i
                v = cells[k]
                if v == 0:
                    new += 1
                    self.worked += 1
                elif v == 1:
                    self.double += 1
                if v < 2:
                    cells[k] = v + 1
                    self.dirty.add((i // self.BLOCK, j // self.BLOCK))
        return new

    def fraction(self, points):
        """Fraction of the points on already worked cells."""
        if not points:
            return 0.0
        return sum(1 for e, n in points if self.count(e, n) > 0) / len(points)

    def block(self, bi, bj):
        B = self.BLOCK
        out = bytearray(B * B)
        for y in range(B):
            j = bj * B + y
            if j >= self.h:
                break
            i0 = bi * B
            i1 = min(self.w, i0 + B)
            out[y * B:y * B + (i1 - i0)] = self.cells[j * self.w + i0:j * self.w + i1]
        return base64.b64encode(bytes(out)).decode()

    def take_dirty(self, limit=64):
        blocks = []
        for key in list(self.dirty)[:limit]:
            self.dirty.discard(key)
            blocks.append([key[0], key[1], self.block(*key)])
        return blocks

    def all_blocks(self):
        B = self.BLOCK
        out = []
        for bj in range((self.h + B - 1) // B):
            for bi in range((self.w + B - 1) // B):
                j0, j1 = bj * B, min(self.h, bj * B + B)
                i0, i1 = bi * B, min(self.w, bi * B + B)
                if any(any(self.cells[j * self.w + i0:j * self.w + i1]) for j in range(j0, j1)):
                    out.append([bi, bj, self.block(bi, bj)])
        return out

    def header(self):
        return {"x0": self.x0, "y0": self.y0, "w": self.w, "h": self.h, "cell": self.cell, "block": self.BLOCK}

    def runs(self):
        """Worked cells as rectangles per row: [(e0, n0, e1, n1)] (for the GeoJSON export)."""
        out = []
        c = self.cell
        for j in range(self.h):
            row = self.cells[j * self.w:(j + 1) * self.w]
            i = 0
            while i < self.w:
                if row[i]:
                    k = i
                    while k < self.w and row[k]:
                        k += 1
                    out.append((self.x0 + i * c, self.y0 + j * c, self.x0 + k * c, self.y0 + (j + 1) * c))
                    i = k
                else:
                    i += 1
        return out

    def area(self):
        return self.worked * self.cell * self.cell

    def overlap_area(self):
        return self.double * self.cell * self.cell


class Autopilot:
    """Drives over a rectangular field in lanes along its long side: lane 1 along the left edge, a U-turn outside the
    field (a half circle), the next lane back, … The pose comes from the distance driven along that path; `step` moves
    the simulated tractor (Sim.live lat, lon, heading)."""

    def __init__(self, field, spacing, headland=None):
        if not field.rect:
            raise ValueError("automatisch rijden kan enkel op een rechthoekig perceel")
        self.field = field
        self.spacing = max(1.0, float(spacing))
        r = field.rect
        self.heading = r["heading"]
        self.length = r["length"]
        self.lanes = max(1, int(math.ceil(r["width"] / self.spacing - 1e-6)))
        self.radius = self.spacing / 2.0
        self.over = self.radius if headland is None else float(headland)  # how far past the field edge before the turn
        self.s = 0.0
        self.finished = False

    def _lane_y(self, k):
        # centre of lane k (0-based) from the left edge, the last lane may stick out a little
        return min(self.field.rect["width"] - self.spacing / 2.0, (k + 0.5) * self.spacing) if self.lanes > 1 else self.field.rect["width"] / 2.0

    @property
    def lane_len(self):
        return self.length + 2 * self.over

    def total(self):
        return self.lanes * self.lane_len + (self.lanes - 1) * math.pi * self.radius

    def pose(self, s):
        """(e, n, heading) at distance s along the path."""
        fx, fy = forward(self.heading)
        rx, ry = right(self.heading)
        seg = self.lane_len + math.pi * self.radius
        k = min(self.lanes - 1, int(s // seg)) if s > 0 else 0
        t = s - k * seg
        up = k % 2 == 0  # even lanes drive in the field direction
        y = self._lane_y(k)
        if t <= self.lane_len or k == self.lanes - 1:
            t = min(t, self.lane_len)
            x = -self.over + t if up else self.length + self.over - t
            h = self.heading if up else (self.heading + 180.0) % 360.0
            return fx * x + rx * y, fy * x + ry * y, h
        # U-turn: half circle to the next lane, outside the field
        a = (t - self.lane_len) / self.radius  # 0 … π
        y2 = self._lane_y(k + 1)
        rr = (y2 - y) / 2.0
        cy = y + rr
        if up:
            cx = self.length + self.over
            x = cx + rr * math.sin(a)
            yy = cy - rr * math.cos(a)
            h = (self.heading + math.degrees(a) * (1 if rr > 0 else -1)) % 360.0
        else:
            cx = -self.over
            x = cx - rr * math.sin(a)
            yy = cy - rr * math.cos(a)
            h = (self.heading + 180.0 - math.degrees(a) * (1 if rr > 0 else -1)) % 360.0
        return fx * x + rx * yy, fy * x + ry * yy, h

    def start(self, sim):
        """Puts the tractor at the start of the first lane."""
        self.s = 0.0
        self.finished = False
        self._apply(sim)

    def _apply(self, sim):
        e, n, h = self.pose(self.s)
        lat, lon = self.field.frame.to_geo(e, n)
        sim.live["lat"], sim.live["lon"], sim.live["heading"] = lat, lon, h

    def step(self, sim, dt):
        if self.finished:
            return
        self.s += sim.ground_speed * dt
        if self.s >= self.total():
            self.s = self.total()
            self.finished = True
        self._apply(sim)
