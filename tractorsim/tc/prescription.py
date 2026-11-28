"""Prescription maps (TC-GEO): zones with a rate, read from GeoJSON or a shapefile, or a demo map over the field.

Zones keep their WGS84 coordinates; `local(frame)` gives them in the metres of a field. The rate is in the unit of the
map (for example L/ha); the task controller converts it to the raw value of the setpoint DDI with the presentation of
the implement.
"""
import io
import json
import math
import re
import struct
import zipfile

from .field import inside_polygon

RATE_NAMES = re.compile(r"rate|dosis|dose|target|value|waarde|amount|hoeveel|n_|kg|l_?ha", re.I)


class MapError(ValueError):
    pass


def _ring_clockwise(ring):
    a = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i - 1]
        x2, y2 = ring[i]
        a += (x2 - x1) * (y2 + y1)
    return a > 0


class Prescription:
    """zones: [{"rings": [[(lat, lon), …], holes…], "rate": float}]"""

    def __init__(self, name, zones, unit="", prop="", default=None):
        self.name = name
        self.zones = zones
        self.unit = unit
        self.prop = prop
        self.default = default
        self._frame = None
        self._local = []

    def local(self, frame):
        if frame is not self._frame:
            self._frame = frame
            self._local = []
            for z in self.zones:
                rings = [[frame.to_local(lat, lon) for lat, lon in r] for r in z["rings"]]
                xs = [p[0] for p in rings[0]]
                ys = [p[1] for p in rings[0]]
                self._local.append({"rings": rings, "rate": z["rate"], "box": (min(xs), min(ys), max(xs), max(ys))})
        return self._local

    def rate_at(self, frame, e, n):
        for z in self.local(frame):
            x0, y0, x1, y1 = z["box"]
            if x0 <= e <= x1 and y0 <= n <= y1 and inside_polygon((e, n), z["rings"]):
                return z["rate"]
        return self.default

    def rates(self):
        return [z["rate"] for z in self.zones]

    def to_json(self, frame):
        return {"name": self.name, "unit": self.unit, "prop": self.prop, "default": self.default,
                "zones": [{"rings": [[[round(x, 2), round(y, 2)] for x, y in r] for r in z["rings"]], "rate": z["rate"]}
                          for z in self.local(frame)]}

    def save(self):
        """GeoJSON text (to keep the map across restarts)."""
        feats = [{"type": "Feature", "properties": {"rate": z["rate"]},
                  "geometry": {"type": "Polygon", "coordinates": [[[lon, lat] for lat, lon in r] for r in z["rings"]]}}
                 for z in self.zones]
        return json.dumps({"type": "FeatureCollection", "name": self.name, "unit": self.unit, "default": self.default,
                           "features": feats})

    # ------------------------------------------------------------ reading
    @staticmethod
    def numeric_props(features):
        props = {}
        for f in features:
            for k, v in (f.get("properties") or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) or (isinstance(v, str) and _num(v) is not None):
                    props[k] = props.get(k, 0) + 1
        return [k for k, _ in sorted(props.items(), key=lambda kv: -kv[1])]

    @classmethod
    def pick_prop(cls, features, prop=None):
        props = cls.numeric_props(features)
        if prop:
            if prop not in props:
                raise MapError(f"eigenschap '{prop}' niet gevonden (wel: {', '.join(props) or 'geen getallen'})")
            return prop, props
        if not props:
            raise MapError("de kaart heeft geen numerieke eigenschap voor de dosis")
        named = [p for p in props if RATE_NAMES.search(p)]
        return (named or props)[0], props

    @classmethod
    def from_features(cls, features, name, prop=None, unit=""):
        prop, props = cls.pick_prop(features, prop)
        zones = []
        for f in features:
            g = f.get("geometry") or {}
            v = _num((f.get("properties") or {}).get(prop))
            if v is None:
                continue
            polys = [g["coordinates"]] if g.get("type") == "Polygon" else g.get("coordinates", []) if g.get("type") == "MultiPolygon" else []
            for poly in polys:
                rings = [[(float(p[1]), float(p[0])) for p in ring] for ring in poly if len(ring) >= 3]
                if rings:
                    _check_wgs84(rings)
                    zones.append({"rings": rings, "rate": v})
        if not zones:
            raise MapError("geen polygonen met een dosis in de kaart")
        p = cls(name, zones, unit, prop)
        p.props = props
        return p

    @classmethod
    def from_geojson(cls, data, name="Taakkaart", prop=None, unit=""):
        try:
            obj = json.loads(data) if isinstance(data, (str, bytes, bytearray)) else data
        except ValueError as e:
            raise MapError(f"geen geldige GeoJSON: {e}") from None
        if obj.get("type") == "FeatureCollection":
            feats = obj.get("features") or []
        elif obj.get("type") == "Feature":
            feats = [obj]
        else:
            raise MapError("GeoJSON zonder Feature of FeatureCollection")
        p = cls.from_features(feats, obj.get("name") or name, prop or None, unit or obj.get("unit", ""))
        if isinstance(obj.get("default"), (int, float)):
            p.default = obj["default"]
        return p

    @classmethod
    def from_shapefile(cls, data, name="Taakkaart", prop=None, unit=""):
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise MapError("een shapefile moet als .zip (met .shp, .dbf en .prj)") from None
        files = {n.lower().rsplit(".", 1)[-1]: n for n in z.namelist() if "." in n and not n.startswith("__MACOSX")}
        if "shp" not in files or "dbf" not in files:
            raise MapError("in de zip ontbreekt een .shp of .dbf")
        if "prj" in files:
            prj = z.read(files["prj"]).decode("latin-1").upper()
            if prj.startswith("PROJCS") or "PROJCS[" in prj:
                raise MapError("de shapefile gebruikt een geprojecteerd coördinatenstelsel: exporteer in WGS84 (EPSG:4326)")
        shapes = _read_shp(z.read(files["shp"]))
        records = _read_dbf(z.read(files["dbf"]))
        feats = []
        for i, rings in enumerate(shapes):
            props = records[i] if i < len(records) else {}
            polys = []
            for r in rings:  # outer rings are clockwise in a shapefile (x = lon, y = lat), holes counter-clockwise
                if _ring_clockwise([(lon, lat) for lat, lon in r]) or not polys:
                    polys.append([r])
                else:
                    polys[-1].append(r)
            coords = [[[[lon, lat] for lat, lon in r] for r in poly] for poly in polys]
            feats.append({"properties": props, "geometry": {"type": "MultiPolygon", "coordinates": coords}})
        return cls.from_features(feats, files["shp"].rsplit("/", 1)[-1].rsplit(".", 1)[0] or name, prop or None, unit)

    @classmethod
    def demo(cls, field, low, high, cell=24.0, unit=""):
        """A grid over the field with rates that vary smoothly between low and high."""
        x0, y0, x1, y1 = field.bbox()
        h = math.radians((field.rect or {}).get("heading", 0.0))
        zones = []
        ny = max(1, int(math.ceil((y1 - y0) / cell)))
        nx = max(1, int(math.ceil((x1 - x0) / cell)))
        for j in range(ny):
            for i in range(nx):
                ex0, ny0 = x0 + i * cell, y0 + j * cell
                cx, cy = ex0 + cell / 2, ny0 + cell / 2
                if not any(field.contains(cx + dx, cy + dy) for dx in (-cell / 3, cell / 3) for dy in (-cell / 3, cell / 3)):
                    continue
                # a smooth pattern along and across the lanes
                u = (cx * math.sin(h) + cy * math.cos(h)) / 90.0
                v = (cx * math.cos(h) - cy * math.sin(h)) / 60.0
                t = 0.5 + 0.35 * math.sin(u * 2.1 + 0.6) + 0.15 * math.cos(v * 2.7)
                rate = low + (high - low) * min(1.0, max(0.0, t))
                step = (high - low) / 8.0 or 1.0
                rate = round(low + round((rate - low) / step) * step, 2)
                ring = [(ex0, ny0), (ex0 + cell, ny0), (ex0 + cell, ny0 + cell), (ex0, ny0 + cell)]
                zones.append({"rings": [[field.frame.to_geo(e, n) for e, n in ring]], "rate": rate})
        p = cls("Demokaart", zones, unit, "rate", default=None)
        p.props = ["rate"]
        return p


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    try:
        f = float(str(v).strip().replace(",", "."))
        return f if math.isfinite(f) else None
    except ValueError:
        return None


def _check_wgs84(rings):
    for r in rings:
        for lat, lon in r:
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise MapError("coördinaten buiten WGS84 (graden): exporteer de kaart in EPSG:4326")


def _read_shp(b):
    """Polygons (types 5, 15, 25) of a .shp file: per record a list of rings [(lat, lon)]."""
    if len(b) < 100 or struct.unpack(">i", b[0:4])[0] != 9994:
        raise MapError("geen geldig .shp-bestand")
    kind = struct.unpack("<i", b[32:36])[0]
    if kind not in (5, 15, 25):
        raise MapError(f"shapefile van type {kind}: enkel polygonen (type 5) worden ondersteund")
    out = []
    p = 100
    while p + 8 <= len(b):
        length = struct.unpack(">i", b[p + 4:p + 8])[0] * 2
        rec = b[p + 8:p + 8 + length]
        p += 8 + length
        if len(rec) < 44 or struct.unpack("<i", rec[0:4])[0] == 0:
            out.append([])
            continue
        nparts, npoints = struct.unpack("<ii", rec[36:44])
        parts = list(struct.unpack(f"<{nparts}i", rec[44:44 + 4 * nparts])) + [npoints]
        base = 44 + 4 * nparts
        pts = [struct.unpack("<dd", rec[base + 16 * k:base + 16 * k + 16]) for k in range(npoints)]
        out.append([[(y, x) for x, y in pts[parts[i]:parts[i + 1]]] for i in range(nparts)])
    return out


def _read_dbf(b):
    if len(b) < 32:
        return []
    n, hlen, rlen = struct.unpack("<IHH", b[4:12])
    fields = []
    p = 32
    while p + 32 <= hlen and b[p] != 0x0D:
        fname = b[p:p + 11].split(b"\0")[0].decode("latin-1").strip()
        fields.append((fname, chr(b[p + 11]), b[p + 16]))
        p += 32
    out = []
    for i in range(n):
        r = b[hlen + i * rlen:hlen + (i + 1) * rlen]
        if not r:
            break
        rec = {}
        q = 1
        for fname, ftype, flen in fields:
            raw = r[q:q + flen].decode("latin-1").strip()
            q += flen
            rec[fname] = _num(raw) if ftype in "NFB" and _num(raw) is not None else raw
        out.append(rec)
    return out
