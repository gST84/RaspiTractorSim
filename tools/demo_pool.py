"""Object pool of the demo implement (tools/fake_implement.py): two data masks, an alarm mask, soft keys,
a meter, bar graphs, inputs, buttons, a hidden container, macros and two pictures (pool > 1785 bytes, so the
upload goes over ETP).

    python3 tools/demo_pool.py [uitvoer.iop]   writes the pool so you can open it in PLC-VT-Designer or on the VT page.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tractorsim.vt.objects import NONE, encode_pool  # noqa: E402

# object IDs the implement uses at run time
WS, MASK_MAIN, MASK_INFO, MASK_ALARM = 0, 1001, 1002, 2001
SKM_MAIN, SKM_INFO, SKM_ALARM = 4001, 4002, 4003
VAR_RPM, VAR_SPEED, VAR_PTO, VAR_HITCH, VAR_RATE = 21001, 21002, 21003, 21004, 21005
STATUS, STOP_BOX, LED_FILL, ARROW = 11010, 3001, 25003, 16001
INFO_LINES = [11030, 11031, 11032, 11033, 11034, 11035]
IN_RATE, IN_MODE, IN_SECTIONS, IN_FIELD, BTN_UP, BTN_DOWN = 9001, 10001, 7001, 8001, 6001, 6002
KEY_UP, KEY_DOWN, KEY_PTO, KEY_INFO, KEY_ALARM, KEY_SPRAY, KEY_RESET, KEY_BACK, KEY_OK = 1, 2, 3, 4, 5, 6, 7, 10, 20


def _picture():
    """120 × 60, 8 bit per pixel: a tractor in a field."""
    w, h = 120, 60
    sky, field, soil, red, black, glass, grey, yellow = 117, 70, 137, 196, 0, 159, 8, 226
    px = bytearray()
    for y in range(h):
        for x in range(w):
            c = sky if y < 34 else field if y < 50 else soil
            if 18 <= x <= 78 and 22 <= y <= 40:
                c = red  # body
            if 52 <= x <= 76 and 8 <= y <= 24:
                c = glass if (55 <= x <= 73 and 11 <= y <= 22) else red  # cab
            if 22 <= x <= 26 and 12 <= y <= 22:
                c = grey  # exhaust
            if (x - 34) ** 2 + (y - 44) ** 2 <= 110:
                c = black if (x - 34) ** 2 + (y - 44) ** 2 > 25 else yellow  # front wheel
            if (x - 68) ** 2 + (y - 41) ** 2 <= 230:
                c = black if (x - 68) ** 2 + (y - 41) ** 2 > 50 else yellow  # rear wheel
            if 84 <= x <= 116 and 34 <= y <= 38 and (x - 84) % 6 < 3:
                c = grey  # sprayer boom
            px.append(c)
    return w, h, bytes(px)


def build(dm=480, sk=80):
    """Pool for a data mask of dm × dm pixels and soft keys of sk × sk."""
    f = dm / 480.0
    g = sk / 80.0
    S = lambda v: int(round(v * f))
    K = lambda v: int(round(v * g))
    objs = []

    def add(oid, typ, **a):
        o = {"id": oid, "type": typ, "macros": []}
        o.update(a)
        objs.append(o)
        return o

    def text(oid, w, h, value, font, just=0, length=None, bg=1, transparent=True, wrap=False, var=NONE):
        return add(oid, 11, width=w, height=h, bgColour=bg, fontAttributes=font, options=(1 if transparent else 0) | (2 if wrap else 0),
                   variableReference=var, justification=just, value=value, length=length or len(value))

    def num(oid, w, h, var, font, scale=1.0, decimals=0, just=2, bg=1):
        return add(oid, 12, width=w, height=h, bgColour=bg, fontAttributes=font, options=1, variableReference=var, value=0,
                   offset=0, scale=scale, decimals=decimals, format=0, justification=just)

    # fonts / line / fill attributes
    for oid, col, size, style in ((23001, 1, 3, 1), (23002, 0, 2, 0), (23003, 0, 3, 0), (23004, 0, 6, 0), (23005, 12, 6, 1),
                                  (23006, 1, 2, 0), (23007, 15, 3, 1)):
        if f < 0.95:  # smaller mask: one font size smaller (8x12 → 8x8, 12x16 → 8x12, 24x32 → 16x24)
            size = {2: 1, 3: 2, 6: 5}.get(size, size)
        add(oid, 23, fontColour=col, fontSize=size, fontType=0, fontStyle=style)
    add(24001, 24, lineColour=0, lineWidth=1, lineArt=0xFFFF)
    add(24002, 24, lineColour=0, lineWidth=0, lineArt=0xFFFF)
    add(24003, 24, lineColour=8, lineWidth=1, lineArt=0xCCCC)
    add(25001, 25, fillType=2, fillColour=15, fillPattern=NONE)
    add(25002, 25, fillType=2, fillColour=7, fillPattern=NONE)
    add(LED_FILL, 25, fillType=2, fillColour=2, fillPattern=NONE)
    add(25004, 25, fillType=2, fillColour=12, fillPattern=NONE)
    add(25005, 25, fillType=2, fillColour=14, fillPattern=NONE)
    add(26001, 26, validationType=0, validationString="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 -_.")

    # variables
    for v in (VAR_RPM, VAR_SPEED, VAR_PTO, VAR_HITCH):
        add(v, 21, value=0)
    add(VAR_RATE, 21, value=150)

    # pictures (same image, two display widths)
    pw, ph, px = _picture()
    raw = base64.b64encode(px).decode()
    add(20001, 20, width=S(240), actualWidth=pw, actualHeight=ph, format=2, options=0, transparencyColour=0, raw=raw)
    add(20002, 20, width=K(72), actualWidth=pw, actualHeight=ph, format=2, options=0, transparencyColour=0, raw=raw)

    # ----- header (shared by both data masks) and the hidden STOP banner
    add(14001, 14, lineAttributes=24002, width=dm, height=S(44), lineSuppression=0, fillAttributes=25001)
    text(11001, S(456), S(24), "Demo-veldspuit  -  Werkscherm", 23001, length=32)
    text(11005, S(456), S(24), "Demo-veldspuit  -  Info", 23001, length=32)
    add(14002, 14, lineAttributes=24002, width=dm, height=S(44), lineSuppression=0, fillAttributes=25004)
    text(11004, S(456), S(24), "STOP: werktuigen uitgeschakeld", 23001, just=1, length=32)
    add(STOP_BOX, 3, width=dm, height=S(44), hidden=1, children=[{"id": 14002, "x": 0, "y": 0}, {"id": 11004, "x": S(12), "y": S(10)}])

    # ----- main data mask
    add(17001, 17, width=S(150), needleColour=12, borderColour=8, arcColour=0, options=15, ticks=7, startAngle=112, endAngle=158,
        minValue=0, maxValue=3000, variableReference=VAR_RPM, value=0)
    num(12001, S(150), S(20), VAR_RPM, 23003, just=1)
    text(11002, S(150), S(14), "motortoerental rpm", 23002, just=1)
    num(12002, S(200), S(36), VAR_SPEED, 23004, scale=0.01, decimals=1, just=2)
    text(11003, S(200), S(14), "km/h grondsnelheid", 23002, just=2)
    text(STATUS, S(220), S(40), "Wacht op tractor...", 23002, length=48, wrap=True, transparent=False, bg=7)
    add(19001, 19, width=S(110), height=S(110), colour=9, targetLineColour=12, options=1 | 2 | 16, startAngle=112, endAngle=158,
        barWidth=S(14), minValue=0, maxValue=1200, variableReference=VAR_PTO, value=0, targetVariableReference=NONE, targetValue=540)
    num(12003, S(90), S(20), VAR_PTO, 23003, just=0)
    text(11006, S(90), S(14), "aftakas rpm", 23002)
    add(18001, 18, width=S(220), height=S(22), colour=2, targetLineColour=12, options=1 | 4 | 16, ticks=11, minValue=0, maxValue=100,
        variableReference=VAR_HITCH, value=0, targetVariableReference=NONE, targetValue=0)
    text(11007, S(120), S(14), "achterhef %", 23002)
    num(12004, S(90), S(14), VAR_HITCH, 23002, just=2)
    add(ARROW, 16, width=S(36), height=S(36), lineAttributes=24001, fillAttributes=25001, polygonType=0,
        points=[{"x": S(18), "y": 0}, {"x": S(36), "y": S(36)}, {"x": S(18), "y": S(26)}, {"x": 0, "y": S(36)}])
    add(15001, 15, lineAttributes=24001, width=S(30), height=S(30), ellipseType=0, startAngle=0, endAngle=0, fillAttributes=LED_FILL)
    text(11008, S(110), S(14), "werktuig vrij", 23002)
    add(13001, 13, lineAttributes=24003, width=S(456), height=0, lineDirection=0)
    # inputs
    text(11011, S(90), S(20), "Dosering", 23003)
    add(IN_RATE, 9, width=S(110), height=S(26), bgColour=1, fontAttributes=23003, options=0, variableReference=VAR_RATE, value=150,
        minValue=0, maxValue=500, offset=0, scale=1.0, decimals=0, format=0, justification=2 | (1 << 2), options2=1)
    text(11012, S(50), S(20), "l/ha", 23002)
    text(11013, S(90), S(20), "Modus", 23003)
    for oid, t in ((11020, "Auto"), (11021, "Manueel"), (11022, "Uit")):
        text(oid, S(110), S(26), t, 23003, just=1 | (1 << 2), transparent=False, bg=1)
    add(IN_MODE, 10, width=S(110), height=S(26), variableReference=NONE, value=0, items=[11020, 11021, 11022], options=1)
    text(11014, S(90), S(20), "Secties", 23003)
    add(IN_SECTIONS, 7, bgColour=1, width=S(26), foreground=23003, variableReference=NONE, value=1, enabled=1)
    text(11015, S(90), S(20), "Perceel", 23003)
    add(IN_FIELD, 8, width=S(220), height=S(26), bgColour=1, fontAttributes=23003, inputAttributes=26001, options=0,
        variableReference=NONE, justification=0 | (1 << 2), value="Achterveld", length=16, enabled=1)
    text(11016, S(100), S(20), "Hef op", 23001, just=1 | (1 << 2))
    text(11017, S(100), S(20), "Hef neer", 23001, just=1 | (1 << 2))
    add(BTN_UP, 6, width=S(104), height=S(40), bgColour=15, borderColour=8, keyCode=KEY_UP, options=0, children=[{"id": 11016, "x": S(2), "y": S(10)}])
    add(BTN_DOWN, 6, width=S(104), height=S(40), bgColour=15, borderColour=8, keyCode=KEY_DOWN, options=0, children=[{"id": 11017, "x": S(2), "y": S(10)}])

    C = lambda oid, x, y: {"id": oid, "x": S(x), "y": S(y)}
    add(MASK_MAIN, 1, bgColour=1, softKeyMask=SKM_MAIN, children=[
        C(14001, 0, 0), C(11001, 12, 10), C(STOP_BOX, 0, 0),
        C(17001, 40, 52), C(12001, 40, 200), C(11002, 40, 222),
        C(12002, 256, 56), C(11003, 256, 94), C(STATUS, 248, 118),
        C(19001, 248, 168), C(12003, 364, 196), C(11006, 364, 218),
        C(18001, 12, 256), C(11007, 12, 282), C(12004, 142, 282),
        C(ARROW, 364, 238), C(15001, 410, 240), C(11008, 364, 278),
        C(13001, 12, 304),
        C(11011, 12, 318), C(IN_RATE, 102, 314), C(11012, 218, 318),
        C(11013, 12, 352), C(IN_MODE, 102, 348),
        C(11014, 12, 386), C(IN_SECTIONS, 102, 382),
        C(11015, 12, 420), C(IN_FIELD, 102, 416),
        C(BTN_UP, 248, 344), C(BTN_DOWN, 364, 344),
    ])

    # ----- info data mask
    for i, oid in enumerate(INFO_LINES):
        text(oid, S(456), S(20), "-", 23003, length=40)
    add(MASK_INFO, 1, bgColour=1, softKeyMask=SKM_INFO, children=[
        C(14001, 0, 0), C(11005, 12, 10), C(STOP_BOX, 0, 0), C(20001, 120, 56)]
        + [C(oid, 12, 190 + 26 * i) for i, oid in enumerate(INFO_LINES)])

    # ----- alarm mask
    text(11040, S(440), S(40), "TE SNEL!", 23005, just=1)
    text(11041, S(440), S(48), "Maximaal 15 km/h bij het spuiten. Druk op OK.", 23003, just=1, wrap=True, length=48)
    add(MASK_ALARM, 2, bgColour=14, softKeyMask=SKM_ALARM, priority=1, acousticSignal=1,
        children=[C(11040, 20, 140), C(11041, 20, 200)])

    # ----- soft keys
    def key(oid, code, label, bg=7, macros=()):
        lines = label.split("|")
        ch = []
        for i, ln in enumerate(lines):
            tid = 50000 + (oid - 5000) * 10 + i
            text(tid, K(78), K(14), ln, 23002, just=1, length=len(ln))
            ch.append({"id": tid, "x": K(1), "y": K(40 - 7 * len(lines) + 14 * i)})
        add(oid, 5, bgColour=bg, keyCode=code, children=ch, macros=[{"event": e, "macro": m} for e, m in macros])

    key(5001, KEY_UP, "Hef|op")
    key(5002, KEY_DOWN, "Hef|neer")
    key(5003, KEY_PTO, "Aftakas|aan/uit")
    key(5004, KEY_INFO, "Info", macros=[(25, 1)])
    key(5005, KEY_ALARM, "Alarm|test", bg=14)
    key(5006, KEY_SPRAY, "Spuiten|aan/uit", bg=10)
    key(5007, KEY_RESET, "Teller|nul")
    key(5010, KEY_BACK, "Terug", macros=[(25, 2)])
    key(5020, KEY_OK, "OK", bg=10)
    add(SKM_MAIN, 4, bgColour=8, keys=[5001, 5002, 5003, 5004, 5005, 5006, 5007])
    add(SKM_INFO, 4, bgColour=8, keys=[5010])
    add(SKM_ALARM, 4, bgColour=8, keys=[5020])

    # ----- macros: switch masks from the soft keys without the implement
    mask_cmd = lambda m: [0xAD, WS & 255, WS >> 8, m & 255, m >> 8, 0xFF, 0xFF, 0xFF]
    add(1, 28, commands=[mask_cmd(MASK_INFO)])
    add(2, 28, commands=[mask_cmd(MASK_MAIN)])

    # ----- working set (designator: picture + name)
    text(11050, K(80), K(14), "DEMO", 23002, just=1)
    add(WS, 0, bgColour=1, selectable=1, activeMask=MASK_MAIN, languages=["nl", "en"],
        children=[{"id": 20002, "x": K(4), "y": K(14)}, {"id": 11050, "x": 0, "y": K(56)}])
    return objs


def pool_bytes(dm=480, sk=80):
    return encode_pool(build(dm, sk))


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_pool.iop")
    data = pool_bytes()
    with open(out, "wb") as fh:
        fh.write(data)
    print(f"{out}: {len(data)} bytes, {len(build())} objecten")
