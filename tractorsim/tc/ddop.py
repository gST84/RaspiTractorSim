"""Device descriptor object pool (DDOP, ISO 11783-10): the description of an implement that a TC client sends to the
task controller. Same object layout as web/can/ddop.js and PLC-VT-Designer src/tc/ddop.js:

  DVC device           designator, software version, NAME (8), serial number, structure label (7),
                       localization label (7) [, extended structure label (length + bytes), version 4]
  DET device element   type, designator, element number, parent id, child ids
  DPD process data     DDI, properties, trigger methods, designator, presentation id
  DPT property         DDI, value (int32), designator, presentation id
  DVP presentation     offset (int32), scale (float32), decimals, unit designator

Strings are Latin-1, or UTF-8 when they start with a byte order mark (version 4).
"""
import struct
from collections import Counter

NONE = 0xFFFF
TABLES = (b"DVC", b"DET", b"DPD", b"DPT", b"DVP")
DET_TYPES = {1: "toestel", 2: "functie", 3: "bak / product", 4: "sectie", 5: "eenheid", 6: "koppeling", 7: "navigatiereferentie"}

# DPD properties and trigger methods (bits)
PROP_DEFAULT, PROP_SETTABLE, PROP_CONTROL_SOURCE = 1, 2, 4
TRIG_TIME, TRIG_DISTANCE, TRIG_THRESHOLD, TRIG_CHANGE, TRIG_TOTAL = 1, 2, 4, 8, 16

# DDIs (ISO 11783-11) the task controller itself uses
DDI_WORKING_WIDTH = 67
DDI_TOTAL_AREA = 116
DDI_OFFSET_X, DDI_OFFSET_Y, DDI_OFFSET_Z = 134, 135, 136
DDI_ACTUAL_WORK_STATE = 141
DDI_PRESCRIPTION_STATE = 158
DDI_SECTION_CONTROL_STATE = 160
DDI_ACTUAL_CONDENSED = 161  # 161..176: sections 1-16, 17-32, …
DDI_SETPOINT_WORK_STATE = 289
DDI_SETPOINT_CONDENSED = 290  # 290..305
DDI_DEFAULT_PD = 0xDFFF

# pool error codes of the Object-pool Activate Response (byte 7)
ERR_UNKNOWN_REFERENCE, ERR_OTHER = 0x02, 0x04


class DdopError(ValueError):
    pass


def _text(b):
    if b[:3] == b"\xef\xbb\xbf":
        return b[3:].decode("utf-8", "replace")
    return b.decode("latin-1")


def _enc_text(s):
    try:
        b = s.encode("latin-1")
    except UnicodeEncodeError:
        b = b"\xef\xbb\xbf" + s.encode("utf-8")
    if len(b) > 255:
        raise DdopError(f"tekst te lang: {s[:20]}…")
    return bytes([len(b)]) + b


def parse(data):
    """Objects (dicts) from pool bytes. Raises DdopError on malformed data."""
    b = bytes(data)
    p = 0

    def need(n):
        if p + n > len(b):
            raise DdopError(f"DDOP afgebroken bij byte {p}")

    def u8():
        nonlocal p
        need(1)
        p += 1
        return b[p - 1]

    def raw(n):
        nonlocal p
        need(n)
        p += n
        return b[p - n:p]

    def u16():
        return struct.unpack("<H", raw(2))[0]

    def i32():
        return struct.unpack("<i", raw(4))[0]

    def f32():
        return struct.unpack("<f", raw(4))[0]

    def text():
        return _text(raw(u8()))

    out = []
    while p < len(b):
        start = p
        table = raw(3)
        oid = u16()
        if table == b"DVC":
            o = {"table": "DVC", "id": oid, "designator": text(), "softwareVersion": text(), "name": raw(8), "serial": text(),
                 "structureLabel": raw(7), "localization": raw(7)}
            if p < len(b) and b[p:p + 3] not in TABLES:  # version 4: extended structure label
                o["extendedLabel"] = raw(u8())
        elif table == b"DET":
            o = {"table": "DET", "id": oid, "type": u8(), "designator": text(), "number": u16(), "parent": u16()}
            o["children"] = [u16() for _ in range(u16())]
        elif table == b"DPD":
            o = {"table": "DPD", "id": oid, "ddi": u16(), "props": u8(), "triggers": u8(), "designator": text(), "presentation": u16()}
        elif table == b"DPT":
            o = {"table": "DPT", "id": oid, "ddi": u16(), "value": i32(), "designator": text(), "presentation": u16()}
        elif table == b"DVP":
            o = {"table": "DVP", "id": oid, "offset": i32(), "scale": f32(), "decimals": u8(), "unit": text()}
        else:
            raise DdopError(f"onbekend DDOP-object {table!r} bij byte {start}")
        out.append(o)
    return out


def encode(objects):
    """Pool bytes (version 3 layout; an extended structure label is written when present)."""
    w = bytearray()
    for o in objects:
        w += o["table"].encode("ascii") + struct.pack("<H", o["id"])
        t = o["table"]
        if t == "DVC":
            w += _enc_text(o["designator"]) + _enc_text(o.get("softwareVersion", "")) + bytes(o["name"])[:8].ljust(8, b"\0")
            w += _enc_text(o.get("serial", "")) + bytes(o["structureLabel"])[:7].ljust(7, b" ")
            w += bytes(o["localization"])[:7].ljust(7, b"\xff")
            if o.get("extendedLabel"):
                w += bytes([len(o["extendedLabel"])]) + bytes(o["extendedLabel"])
        elif t == "DET":
            w += bytes([o["type"]]) + _enc_text(o["designator"]) + struct.pack("<HHH", o["number"], o["parent"], len(o["children"]))
            w += b"".join(struct.pack("<H", c) for c in o["children"])
        elif t == "DPD":
            w += struct.pack("<HBB", o["ddi"], o["props"], o["triggers"]) + _enc_text(o["designator"]) + struct.pack("<H", o.get("presentation", NONE))
        elif t == "DPT":
            w += struct.pack("<Hi", o["ddi"], int(o["value"])) + _enc_text(o["designator"]) + struct.pack("<H", o.get("presentation", NONE))
        elif t == "DVP":
            w += struct.pack("<ifB", int(o["offset"]), float(o["scale"]), o["decimals"]) + _enc_text(o["unit"])
        else:
            raise DdopError(f"onbekend DDOP-object {t}")
    return bytes(w)


def check(objects):
    """Structural errors: [(parent id, object id, error code, text)], empty = valid."""
    errs = []
    by_id = {}
    for o in objects:
        if o["id"] in by_id:
            errs.append((NONE, o["id"], ERR_OTHER, f"object-ID {o['id']} komt twee keer voor"))
        by_id[o["id"]] = o
    devices = [o for o in objects if o["table"] == "DVC"]
    if len(devices) != 1:
        errs.append((NONE, devices[1]["id"] if len(devices) > 1 else NONE, ERR_OTHER, f"{len(devices)} toestellen (DVC), er moet er één zijn"))
    numbers = set()
    for o in objects:
        if o["table"] == "DET":
            if o["number"] in numbers:
                errs.append((o["parent"], o["id"], ERR_OTHER, f"elementnummer {o['number']} komt twee keer voor"))
            numbers.add(o["number"])
            parent = by_id.get(o["parent"])
            if parent is None or parent["table"] not in ("DET", "DVC"):
                errs.append((o["parent"], o["id"], ERR_UNKNOWN_REFERENCE, f"ouder {o['parent']} van element {o['number']} bestaat niet"))
            for c in o["children"]:
                if by_id.get(c, {}).get("table") not in ("DPD", "DPT"):
                    errs.append((o["id"], c, ERR_UNKNOWN_REFERENCE, f"kind {c} van element {o['number']} is geen procesdata of eigenschap"))
        elif o["table"] in ("DPD", "DPT") and o["presentation"] != NONE and by_id.get(o["presentation"], {}).get("table") != "DVP":
            errs.append((NONE, o["id"], ERR_UNKNOWN_REFERENCE, f"weergave {o['presentation']} van object {o['id']} bestaat niet"))
    return errs


def condensed(states, group):
    """32-bit value of a condensed work state: 2 bits per section (01 on, 00 off, 11 not installed), section 1 lowest.
    `states` is the list of all sections (True/False), `group` 0 for sections 1-16, 1 for 17-32, …"""
    v = 0
    for b in range(16):
        k = group * 16 + b
        v |= (3 if k >= len(states) else (1 if states[k] else 0)) << (2 * b)
    return v - (1 << 32) if v >= 1 << 31 else v


def uncondensed(value, group, count):
    """Section states from a condensed work state: {section index (0-based): True/False/None (error, not installed)}."""
    v = value & 0xFFFFFFFF
    out = {}
    for b in range(16):
        k = group * 16 + b
        if k < count:
            s = (v >> (2 * b)) & 3
            out[k] = True if s == 1 else False if s == 0 else None
    return out


class Pool:
    """An activated DDOP with lookups by element number and DDI."""

    def __init__(self, objects, data=b""):
        self.objects = objects
        self.data = bytes(data)
        self.by_id = {o["id"]: o for o in objects}
        self.device = next(o for o in objects if o["table"] == "DVC")
        self.elements = {}  # element number → DET
        self.number_of = {}  # DET id → element number
        self.dpds = {}  # (element, DDI) → DPD
        self.dpts = {}  # (element, DDI) → DPT
        for o in objects:
            if o["table"] == "DET":
                self.elements[o["number"]] = o
                self.number_of[o["id"]] = o["number"]
        for o in objects:
            if o["table"] != "DET":
                continue
            for c in o["children"]:
                x = self.by_id.get(c)
                if x and x["table"] == "DPD":
                    self.dpds.setdefault((o["number"], x["ddi"]), x)
                elif x and x["table"] == "DPT":
                    self.dpts.setdefault((o["number"], x["ddi"]), x)

    @property
    def label(self):
        return bytes(self.device["structureLabel"])

    @property
    def localization(self):
        return bytes(self.device["localization"])

    @property
    def designator(self):
        return self.device["designator"]

    def parent(self, number):
        """Element number of the parent element, or None (the device)."""
        e = self.elements.get(number)
        return self.number_of.get(e["parent"]) if e else None

    def dpt_value(self, number, ddi):
        o = self.dpts.get((number, ddi))
        return o["value"] if o else None

    def presentation(self, obj):
        p = obj.get("presentation", NONE)
        o = self.by_id.get(p) if p != NONE else None
        return o if o and o["table"] == "DVP" else None

    def find(self, ddi):
        """[(element, DPD)] with this DDI."""
        return [(k[0], o) for k, o in self.dpds.items() if k[1] == ddi]

    def first(self, ddi):
        f = self.find(ddi)
        return f[0][0] if f else None

    def sections(self):
        """Geometry for section control: {"boom": element, "sections": [{index, element, x, y, w}]} in mm relative to
        the connector (x forward, y to the right), or None without section elements. The boom is the parent with the most
        sections; the order is the order in the pool (that of the condensed work states). Missing y or width: None."""
        secs = [o for o in self.objects if o["table"] == "DET" and o["type"] == 4]
        if not secs:
            return None
        boom = Counter(self.number_of.get(s["parent"]) for s in secs).most_common(1)[0][0]
        secs = [s for s in secs if self.number_of.get(s["parent"]) == boom]
        conn = next((o["number"] for o in self.objects if o["table"] == "DET" and o["type"] == 6), None)
        cx = (self.dpt_value(conn, DDI_OFFSET_X) or 0) if conn is not None else 0
        cy = (self.dpt_value(conn, DDI_OFFSET_Y) or 0) if conn is not None else 0
        bx = self.dpt_value(boom, DDI_OFFSET_X) if boom is not None else None
        out = []
        for i, s in enumerate(secs):
            n = s["number"]
            x = self.dpt_value(n, DDI_OFFSET_X)
            x = bx if x is None else x
            y = self.dpt_value(n, DDI_OFFSET_Y)
            out.append({"index": i, "element": n, "x": (x or 0) - cx, "y": None if y is None else y - cy,
                        "w": self.dpt_value(n, DDI_WORKING_WIDTH)})
        return {"boom": boom, "sections": out}

    def to_json(self):
        """Objects for the web page (bytes as lists)."""
        return [{k: (list(v) if isinstance(v, (bytes, bytearray)) else v) for k, v in o.items()} for o in self.objects]
