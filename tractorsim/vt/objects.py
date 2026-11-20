"""ISO 11783-6 object pool: definitions, parser and encoder.

The field lists follow the byte order of annex B exactly and are the same as src/iso/schema.js of
PLC-VT-Designer (VT version 3 with the version 4 options), plus the version 4 objects Window Mask,
Key Group, Graphics Context, Output List, Extended Input Attributes, Colour Map and Object Label
Reference List. Parsed objects are plain dicts {id, type, ...attributes} with the attribute names of
schema.js, so the browser can render them with the designer's renderer.
"""
import base64
import struct

NONE = 0xFFFF


# field constructors: (kind, name, extra)
def U8(n): return ("u8", n)
def U16(n): return ("u16", n)
def I16(n): return ("i16", n)
def U32(n): return ("u32", n)
def I32(n): return ("i32", n)
def F32(n): return ("f32", n)
def REF(n): return ("ref", n)
def CNT(of, size=1): return ("count", of, size)
def STR(n, len_size, fixed): return ("string", n, len_size, fixed)
def LIST(kind, n=None): return (kind, n)


MAC = [CNT("macros")]
CH = [CNT("children"), CNT("macros")]
TAIL_CH = [LIST("children", "children"), LIST("macros", "macros")]
TAIL_MAC = [LIST("macros", "macros")]
W, H, BG = U16("width"), U16("height"), U8("bgColour")

TYPES = {
    0: ("Working Set", [BG, U8("selectable"), REF("activeMask"), CNT("children"), CNT("macros"), CNT("languages"),
                        LIST("children", "children"), LIST("macros", "macros"), LIST("langs", "languages")]),
    1: ("Data Mask", [BG, REF("softKeyMask"), *CH, *TAIL_CH]),
    2: ("Alarm Mask", [BG, REF("softKeyMask"), U8("priority"), U8("acousticSignal"), *CH, *TAIL_CH]),
    3: ("Container", [W, H, U8("hidden"), *CH, *TAIL_CH]),
    4: ("Soft Key Mask", [BG, CNT("keys"), *MAC, LIST("idlist", "keys"), *TAIL_MAC]),
    5: ("Key", [BG, U8("keyCode"), *CH, *TAIL_CH]),
    6: ("Button", [W, H, BG, U8("borderColour"), U8("keyCode"), U8("options"), *CH, *TAIL_CH]),
    7: ("Input Boolean", [BG, U16("width"), REF("foreground"), REF("variableReference"), U8("value"), U8("enabled"), *MAC, *TAIL_MAC]),
    8: ("Input String", [W, H, BG, REF("fontAttributes"), REF("inputAttributes"), U8("options"), REF("variableReference"),
                         U8("justification"), STR("value", 1, True), U8("enabled"), *MAC, *TAIL_MAC]),
    9: ("Input Number", [W, H, BG, REF("fontAttributes"), U8("options"), REF("variableReference"), U32("value"),
                         U32("minValue"), U32("maxValue"), I32("offset"), F32("scale"), U8("decimals"), U8("format"),
                         U8("justification"), U8("options2"), *MAC, *TAIL_MAC]),
    10: ("Input List", [W, H, REF("variableReference"), U8("value"), CNT("items"), U8("options"), *MAC,
                        LIST("idlist", "items"), *TAIL_MAC]),
    11: ("Output String", [W, H, BG, REF("fontAttributes"), U8("options"), REF("variableReference"), U8("justification"),
                           STR("value", 2, True), *MAC, *TAIL_MAC]),
    12: ("Output Number", [W, H, BG, REF("fontAttributes"), U8("options"), REF("variableReference"), U32("value"),
                           I32("offset"), F32("scale"), U8("decimals"), U8("format"), U8("justification"), *MAC, *TAIL_MAC]),
    13: ("Output Line", [REF("lineAttributes"), W, H, U8("lineDirection"), *MAC, *TAIL_MAC]),
    14: ("Output Rectangle", [REF("lineAttributes"), W, H, U8("lineSuppression"), REF("fillAttributes"), *MAC, *TAIL_MAC]),
    15: ("Output Ellipse", [REF("lineAttributes"), W, H, U8("ellipseType"), U8("startAngle"), U8("endAngle"),
                            REF("fillAttributes"), *MAC, *TAIL_MAC]),
    16: ("Output Polygon", [W, H, REF("lineAttributes"), REF("fillAttributes"), U8("polygonType"), CNT("points"), *MAC,
                            LIST("points", "points"), *TAIL_MAC]),
    17: ("Output Meter", [U16("width"), U8("needleColour"), U8("borderColour"), U8("arcColour"), U8("options"), U8("ticks"),
                          U8("startAngle"), U8("endAngle"), U16("minValue"), U16("maxValue"), REF("variableReference"),
                          U16("value"), *MAC, *TAIL_MAC]),
    18: ("Output Linear Bar Graph", [W, H, U8("colour"), U8("targetLineColour"), U8("options"), U8("ticks"), U16("minValue"),
                                     U16("maxValue"), REF("variableReference"), U16("value"), REF("targetVariableReference"),
                                     U16("targetValue"), *MAC, *TAIL_MAC]),
    19: ("Output Arched Bar Graph", [W, H, U8("colour"), U8("targetLineColour"), U8("options"), U8("startAngle"),
                                     U8("endAngle"), U16("barWidth"), U16("minValue"), U16("maxValue"), REF("variableReference"),
                                     U16("value"), REF("targetVariableReference"), U16("targetValue"), *MAC, *TAIL_MAC]),
    20: ("Picture Graphic", [U16("width"), U16("actualWidth"), U16("actualHeight"), U8("format"), U8("options"),
                             U8("transparencyColour"), CNT("$raw", 4), *MAC, LIST("picdata", "raw"), *TAIL_MAC]),
    21: ("Number Variable", [U32("value")]),
    22: ("String Variable", [STR("value", 2, True)]),
    23: ("Font Attributes", [U8("fontColour"), U8("fontSize"), U8("fontType"), U8("fontStyle"), *MAC, *TAIL_MAC]),
    24: ("Line Attributes", [U8("lineColour"), U8("lineWidth"), U16("lineArt"), *MAC, *TAIL_MAC]),
    25: ("Fill Attributes", [U8("fillType"), U8("fillColour"), REF("fillPattern"), *MAC, *TAIL_MAC]),
    26: ("Input Attributes", [U8("validationType"), STR("validationString", 1, False), *MAC, *TAIL_MAC]),
    27: ("Object Pointer", [REF("value")]),
    28: ("Macro", [CNT("commands", 2), LIST("macrocmds", "commands")]),
    # ----- version 4
    34: ("Window Mask", [U8("width"), U8("height"), U8("windowType"), BG, U8("options"), REF("name"), REF("windowTitle"),
                         REF("windowIcon"), CNT("objects"), CNT("children"), CNT("macros"), LIST("idlist", "objects"), *TAIL_CH]),
    35: ("Key Group", [U8("options"), REF("name"), REF("keyGroupIcon"), CNT("keys"), *MAC, LIST("idlist", "keys"), *TAIL_MAC]),
    36: ("Graphics Context", [U16("viewportWidth"), U16("viewportHeight"), I16("viewportX"), I16("viewportY"),
                              U16("canvasWidth"), U16("canvasHeight"), F32("viewportZoom"), I16("cursorX"), I16("cursorY"),
                              U8("foregroundColour"), BG, REF("fontAttributes"), REF("lineAttributes"),
                              REF("fillAttributes"), U8("format"), U8("options"), U8("transparencyColour")]),
    37: ("Output List", [W, H, REF("variableReference"), U8("value"), CNT("items"), *MAC, LIST("idlist", "items"), *TAIL_MAC]),
    38: ("Extended Input Attributes", [U8("validationType"), LIST("codeplanes", "codePlanes")]),
    39: ("Colour Map", [CNT("colours", 2), LIST("bytes", "colours")]),
    40: ("Object Label Reference List", [CNT("labels", 2), LIST("labels", "labels")]),
}

SCALAR = ("u8", "u16", "i16", "u32", "i32", "f32", "ref")


def type_name(t):
    return TYPES[t][0] if t in TYPES else f"type {t}"


def _aids(t):
    if t == 20:
        return ["width", "options", "transparencyColour", "actualWidth", "actualHeight", "format"]
    return [f[1] for f in TYPES[t][1] if f[0] in SCALAR and f[1] not in ("value", "enabled")]


# Attribute IDs for Change Attribute / Get Attribute Value: AID n = AIDS[type][n - 1].
# Order of annex B attribute tables: the fields without the values that have their own command
# (value, enabled). For pictures the read-only attributes come last. Not checked against the standard.
AIDS = {t: _aids(t) for t in TYPES}
KIND = {t: {f[1]: f[0] for f in TYPES[t][1] if f[0] in SCALAR} for t in TYPES}


def macro_command_length(data, p):
    code = data[p]
    if code == 0xB3:
        return 5 + (data[p + 3] | (data[p + 4] << 8)) if p + 4 < len(data) else len(data) - p
    if code == 0xB4:
        return 9
    return 8


class PoolError(Exception):
    def __init__(self, msg, obj_id=NONE, parent_id=NONE, offset=0):
        super().__init__(msg)
        self.obj_id, self.parent_id, self.offset = obj_id, parent_id, offset


class _Reader:
    def __init__(self, b):
        self.b, self.p = b, 0

    def take(self, n):
        if self.p + n > len(self.b):
            raise PoolError(f"pool eindigt te vroeg (offset {self.p}, nog {n} bytes nodig)")
        v = self.b[self.p:self.p + n]
        self.p += n
        return v

    def u8(self): return self.take(1)[0]
    def u16(self): return struct.unpack("<H", self.take(2))[0]
    def i16(self): return struct.unpack("<h", self.take(2))[0]
    def u32(self): return struct.unpack("<I", self.take(4))[0]
    def i32(self): return struct.unpack("<i", self.take(4))[0]
    def f32(self): return struct.unpack("<f", self.take(4))[0]

    def n(self, size):
        return self.u8() if size == 1 else self.u16() if size == 2 else self.u32()


def parse_pool(data):
    """bytes → list of objects. Raises PoolError (with the faulty object ID) on unknown types or a truncated pool."""
    r = _Reader(bytes(data))
    out = []
    while r.p < len(r.b):
        start = r.p
        oid = r.u16()
        typ = r.u8()
        if typ not in TYPES:
            raise PoolError(f"objecttype {typ} (object {oid}, offset {start}) wordt niet ondersteund", oid, NONE, start)
        obj = {"id": oid, "type": typ}
        counts = {}
        try:
            for f in TYPES[typ][1]:
                k = f[0]
                if k == "u8": obj[f[1]] = r.u8()
                elif k in ("u16", "ref"): obj[f[1]] = r.u16()
                elif k == "i16": obj[f[1]] = r.i16()
                elif k == "u32": obj[f[1]] = r.u32()
                elif k == "i32": obj[f[1]] = r.i32()
                elif k == "f32": obj[f[1]] = r.f32()
                elif k == "count": counts[f[1]] = r.n(f[2])
                elif k == "children":
                    obj["children"] = [{"id": r.u16(), "x": r.i16(), "y": r.i16()} for _ in range(counts["children"])]
                elif k == "macros":
                    obj["macros"] = [{"event": r.u8(), "macro": r.u8()} for _ in range(counts["macros"])]
                elif k == "idlist":
                    obj[f[1]] = [r.u16() for _ in range(counts[f[1]])]
                elif k == "langs":
                    obj["languages"] = [r.take(2).decode("latin-1") for _ in range(counts["languages"])]
                elif k == "points":
                    obj["points"] = [{"x": r.u16(), "y": r.u16()} for _ in range(counts["points"])]
                elif k == "string":
                    ln = r.n(f[2])
                    s = r.take(ln).decode("latin-1")
                    if f[3]:
                        obj["length"] = ln
                        obj[f[1]] = s.rstrip(" ")
                    else:
                        obj[f[1]] = s
                elif k == "picdata":
                    obj["raw"] = base64.b64encode(r.take(counts["$raw"])).decode()
                elif k == "macrocmds":
                    raw = r.take(counts["commands"])
                    cmds, p = [], 0
                    while p < len(raw):
                        n = min(macro_command_length(raw, p), len(raw) - p)
                        cmds.append(list(raw[p:p + n]))
                        p += n
                    obj["commands"] = cmds
                elif k == "bytes":
                    obj[f[1]] = list(r.take(counts[f[1]]))
                elif k == "labels":
                    obj["labels"] = [{"id": r.u16(), "stringVariable": r.u16(), "fontType": r.u8(), "graphic": r.u16()}
                                     for _ in range(counts["labels"])]
                elif k == "codeplanes":
                    planes = []
                    for _ in range(r.u8()):
                        plane = r.u8()
                        planes.append({"plane": plane, "ranges": [[r.u16(), r.u16()] for _ in range(r.u8())]})
                    obj["codePlanes"] = planes
        except PoolError as e:
            raise PoolError(f"object {oid} ({type_name(typ)}, offset {start}): {e}", oid, NONE, start)
        out.append(obj)
    return out


def encode_object(obj):
    typ = obj["type"]
    b = bytearray(struct.pack("<HB", obj["id"], typ))
    raw = base64.b64decode(obj.get("raw", "")) if typ == 20 else b""
    cmds = b"".join(bytes(c) for c in obj.get("commands", [])) if typ == 28 else b""

    def count_of(n):
        if n == "$raw": return len(raw)
        if n == "commands": return len(cmds)
        return len(obj.get(n, []))

    for f in TYPES[typ][1]:
        k = f[0]
        if k == "u8": b += struct.pack("<B", int(obj.get(f[1], 0)) & 0xFF)
        elif k in ("u16", "ref"): b += struct.pack("<H", int(obj.get(f[1], NONE if k == "ref" else 0)) & 0xFFFF)
        elif k == "i16": b += struct.pack("<h", int(obj.get(f[1], 0)))
        elif k == "u32": b += struct.pack("<I", int(obj.get(f[1], 0)) & 0xFFFFFFFF)
        elif k == "i32": b += struct.pack("<i", int(obj.get(f[1], 0)))
        elif k == "f32": b += struct.pack("<f", float(obj.get(f[1], 1.0)))
        elif k == "count": b += count_of(f[1]).to_bytes(f[2], "little")
        elif k == "children":
            for c in obj.get("children", []): b += struct.pack("<Hhh", c["id"], c["x"], c["y"])
        elif k == "macros":
            for m in obj.get("macros", []): b += struct.pack("<BB", m["event"], m["macro"])
        elif k == "idlist":
            for i in obj.get(f[1], []): b += struct.pack("<H", i)
        elif k == "langs":
            for lang in obj.get("languages", []): b += lang.encode("latin-1")[:2].ljust(2, b" ")
        elif k == "points":
            for p in obj.get("points", []): b += struct.pack("<HH", p["x"], p["y"])
        elif k == "string":
            s = str(obj.get(f[1], ""))
            if f[3]:
                s = s[: obj.get("length", len(s))].ljust(obj.get("length", len(s)))
            enc = s.encode("latin-1", "replace")
            b += len(enc).to_bytes(f[2], "little") + enc
        elif k == "picdata": b += raw
        elif k == "macrocmds": b += cmds
        elif k == "bytes": b += bytes(obj.get(f[1], []))
        elif k == "labels":
            for lb in obj.get("labels", []): b += struct.pack("<HHBH", lb["id"], lb["stringVariable"], lb["fontType"], lb["graphic"])
        elif k == "codeplanes":
            planes = obj.get("codePlanes", [])
            b += bytes([len(planes)])
            for pl in planes:
                b += bytes([pl["plane"], len(pl["ranges"])])
                for a, z in pl["ranges"]: b += struct.pack("<HH", a, z)
    return bytes(b)


def encode_pool(objects):
    """Working Set first, then by ID (same order as the designer)."""
    objs = sorted(objects, key=lambda o: (o["type"] != 0, o["id"]))
    return b"".join(encode_object(o) for o in objs)


def references(obj):
    """(attribute, referenced ID) for every object reference in an object."""
    out = []
    for f in TYPES[obj["type"]][1]:
        if f[0] == "ref" and obj.get(f[1], NONE) != NONE:
            out.append((f[1], obj[f[1]]))
        elif f[0] == "children":
            out += [("children", c["id"]) for c in obj.get("children", [])]
        elif f[0] == "idlist":
            out += [(f[1], i) for i in obj.get(f[1], []) if i != NONE]
    return out


def validate(objects):
    """Checks a parsed pool. → (errors, warnings); errors are (text, object id, parent id)."""
    by_id = {}
    errors, warnings = [], []
    for o in objects:
        if o["id"] in by_id:
            errors.append((f"object-ID {o['id']} komt twee keer voor", o["id"], NONE))
        by_id[o["id"]] = o
    ws = [o for o in objects if o["type"] == 0]
    if len(ws) != 1:
        errors.append((f"de pool moet precies één Working Set bevatten (gevonden: {len(ws)})", NONE, NONE))
    elif by_id.get(ws[0]["activeMask"], {}).get("type") not in (1, 2):
        errors.append((f"actief masker {ws[0]['activeMask']} van de Working Set bestaat niet", ws[0]["activeMask"], ws[0]["id"]))
    macros = {o["id"] for o in objects if o["type"] == 28}
    for o in objects:
        for attr, rid in references(o):
            if rid not in by_id:
                warnings.append((f"{type_name(o['type'])} {o['id']}: {attr} verwijst naar onbestaand object {rid}", rid, o["id"]))
        for m in o.get("macros", []):
            if m["macro"] not in macros:
                warnings.append((f"{type_name(o['type'])} {o['id']}: macro {m['macro']} bestaat niet", m["macro"], o["id"]))
    return errors, warnings
