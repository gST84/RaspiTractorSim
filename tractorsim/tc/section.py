"""Section control (TC-SC): where the sections of the implement are, which of them should work, and the coverage
they leave behind.

The implement describes its sections in the DDOP (element offsets X/Y and the working width, in mm, relative to its
connector). The task controller adds where the connector sits on the tractor (`hitch`, metres from the GNSS antenna:
x forward, y to the right). A section is switched off when its look-ahead point is outside the field or on ground that
was already worked (at least `overlap` of the section), and on otherwise. The implement follows only in automatic mode
(Section Control State, DDI 160 = 1).
"""
import math

from .field import forward, right

SAMPLES = 5  # points across a section for the look-ahead test


class SectionControl:
    def __init__(self):
        self.hitch = (-2.0, 0.0)
        self.lookahead_on = 0.6  # s: switch on before the section reaches unworked ground
        self.lookahead_off = 0.3  # s: switch off before it reaches worked ground
        self.overlap = 0.5  # fraction of a section on worked ground that switches it off
        self.secs = []  # [{index, element, x, y, w}] in metres relative to the connector
        self.boom = None
        self.decided = []  # what section control wants (True/False per section)
        self._prev = None  # section ends of the previous tick, for painting

    def configure(self, geometry, live_width=None):
        """Sections from Pool.sections(); widths missing in the DDOP come from the live working width (DDI 67 of the
        boom, mm) shared equally, else 3 m. Y missing: sections side by side, centred on the connector."""
        self._prev = None
        if not geometry:
            self.secs, self.boom, self.decided = [], None, []
            return
        raw = geometry["sections"]
        n = len(raw)
        default_w = (live_width / n / 1000.0) if live_width and n else 3.0
        secs = []
        for s in raw:
            secs.append({"index": s["index"], "element": s["element"], "x": s["x"] / 1000.0,
                         "y": None if s["y"] is None else s["y"] / 1000.0,
                         "w": (s["w"] / 1000.0) if s["w"] else default_w})
        if any(s["y"] is None for s in secs):
            total = sum(s["w"] for s in secs)
            y = -total / 2.0
            for s in secs:
                s["y"] = y + s["w"] / 2.0
                y += s["w"]
        self.secs = secs
        self.boom = geometry["boom"]
        if len(self.decided) != n:
            self.decided = [False] * n

    @property
    def width(self):
        return sum(s["w"] for s in self.secs)

    def ends(self, e, n, heading, ahead=0.0):
        """Per section: ((left e, n), (right e, n)) of its working line, `ahead` metres further forward."""
        fx, fy = forward(heading)
        rx, ry = right(heading)
        hx, hy = self.hitch
        out = []
        for s in self.secs:
            x = hx + s["x"] + ahead
            yl = hy + s["y"] - s["w"] / 2.0
            yr = hy + s["y"] + s["w"] / 2.0
            out.append(((e + fx * x + rx * yl, n + fy * x + ry * yl), (e + fx * x + rx * yr, n + fy * x + ry * yr)))
        return out

    def centre(self, e, n, heading, ahead=0.0):
        """Middle of the boom, `ahead` metres further forward."""
        if not self.secs:
            return e, n
        fx, fy = forward(heading)
        rx, ry = right(heading)
        x = self.hitch[0] + sum(s["x"] for s in self.secs) / len(self.secs) + ahead
        y = self.hitch[1] + sum(s["y"] for s in self.secs) / len(self.secs)
        return e + fx * x + rx * y, n + fy * x + ry * y

    def decide(self, e, n, heading, speed, coverage, field):
        """New wanted state per section (list of bool)."""
        if not self.secs:
            return []
        if speed < 0.1:
            self.decided = [False] * len(self.secs)
            return self.decided
        out = []
        for i, s in enumerate(self.secs):
            was = self.decided[i] if i < len(self.decided) else False
            # at least 1 m: the cells just painted by this section itself must not switch it off
            ahead = max(1.0, speed * (self.lookahead_off if was else self.lookahead_on))
            (le_, ln), (re_, rn) = self.ends(e, n, heading, ahead)[i]
            pts = [(le_ + (re_ - le_) * (k + 0.5) / SAMPLES, ln + (rn - ln) * (k + 0.5) / SAMPLES) for k in range(SAMPLES)]
            in_field = sum(1 for p in pts if field.contains(*p)) / SAMPLES if field else 1.0
            worked = coverage.fraction(pts) if coverage else 0.0
            out.append(in_field >= 0.5 and worked < self.overlap)
        self.decided = out
        return out

    def paint(self, e, n, heading, states, coverage):
        """Marks what the working sections covered since the previous tick. → new area (m²)."""
        now = self.ends(e, n, heading)
        prev, self._prev = self._prev, now
        if prev is None or coverage is None or len(prev) != len(now):
            return 0.0
        # a jump (new position, a reset of the simulator): do not paint a long streak
        if math.hypot(now[0][0][0] - prev[0][0][0], now[0][0][1] - prev[0][0][1]) > 15.0:
            return 0.0
        cells = 0
        for i, on in enumerate(states):
            if on and i < len(now):
                (pl, pr), (cl, cr) = prev[i], now[i]
                cells += coverage.paint([pl, pr, cr, cl])
        return cells * coverage.cell * coverage.cell

    def reset(self):
        self._prev = None
