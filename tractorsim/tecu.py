"""Tractor ECU simulation: settable values, the broadcast messages of a TECU (ISO 11783-7 / SAE J1939-71),
NMEA 2000 position messages of a GNSS receiver, and the commands an implement sends to the tractor.

Message layouts follow PLC-VT-Designer/isobus_project_bundle (decode.py and ISO_TractorDecode.scl, checked
against a bus log of a real tractor) where available. Messages marked UNVERIFIED in MESSAGES have their
layout from memory of the standard: check them against a log before relying on them.
"""
import asyncio
import datetime
import math
import random
import struct
import time

from .j1939 import make_id

# ------------------------------------------------------------------ settable values
PARAMS = []


def P(key, label, group, kind="num", unit="", lo=0, hi=100, step=1, default=0, options=None, hint=""):
    PARAMS.append({"key": key, "label": label, "group": group, "kind": kind, "unit": unit, "min": lo, "max": hi,
                   "step": step, "default": default, "options": options, "hint": hint})


ST_LIMIT = [[0, "niet begrensd"], [1, "begrensd door bestuurder"], [2, "begrensd hoog"], [3, "begrensd laag"],
            [6, "onherstelbare fout"], [7, "n.b."]]
INWORK = [[-1, "automatisch (drempel)"], [0, "uit werkstand"], [1, "in werkstand"], [3, "n.b."]]

P("engine.rpm", "Toerental", "Motor", unit="rpm", hi=3000, step=10, default=1800)
P("engine.torque", "Actueel koppel", "Motor", unit="%", hi=125, default=45)
P("engine.demandTorque", "Gevraagd koppel", "Motor", unit="%", hi=125, default=50)
P("engine.throttle", "Gaspedaal", "Motor", unit="%", hi=100, default=40)
P("engine.coolant", "Koelvloeistof", "Motor", unit="°C", lo=-40, hi=150, default=85)
P("engine.oilTemp", "Olietemperatuur", "Motor", unit="°C", lo=-40, hi=200, default=95)
P("engine.oilPressure", "Oliedruk", "Motor", unit="kPa", hi=1000, step=4, default=350)
P("engine.fuelRate", "Brandstofverbruik", "Motor", unit="L/h", hi=150, step=0.5, default=18)
P("engine.fuelLevel", "Brandstofniveau", "Motor", unit="%", hi=100, default=75)
P("engine.battery", "Accuspanning", "Motor", unit="V", hi=32, step=0.1, default=13.8)
P("engine.hours", "Motoruren", "Motor", unit="h", hi=99999, step=0.1, default=1234.5, hint="loopt op zolang de motor draait")
P("env.ambient", "Buitentemperatuur", "Motor", unit="°C", lo=-40, hi=60, default=15)
P("env.baro", "Luchtdruk", "Motor", unit="kPa", lo=50, hi=125, step=0.5, default=101)

P("drive.speed", "Rijsnelheid", "Rijden", unit="km/h", hi=60, step=0.1, default=8)
P("drive.direction", "Rijrichting", "Rijden", "select", default=1, options=[[1, "vooruit"], [0, "achteruit"], [3, "n.b."]])
P("drive.slip", "Wielslip", "Rijden", unit="%", lo=-20, hi=50, step=0.5, default=3, hint="wielsnelheid = grondsnelheid × (1 + slip)")
P("drive.source", "Bron machine selected speed", "Rijden", "select", default=1,
  options=[[0, "wiel"], [1, "grond (radar)"], [2, "navigatie"], [3, "gemengd"], [4, "gesimuleerd"]])
P("drive.speedLimit", "Snelheidsbegrenzing", "Rijden", "select", default=0, options=ST_LIMIT)
P("drive.gear", "Versnelling", "Rijden", lo=-10, hi=40, default=8)
P("drive.key", "Contactsleutel", "Rijden", "select", default=1, options=[[1, "aan"], [0, "uit"], [3, "n.b."]])
P("drive.startStop", "Implement start/stop", "Rijden", "select", default=1, options=[[1, "start (werken)"], [0, "stop"], [3, "n.b."]])
P("drive.reversed", "Bediening achteruitgedraaid", "Rijden", "select", default=0, options=[[0, "nee"], [1, "ja (omkeerbare cabine)"], [3, "n.b."]])
P("drive.maxPowerTime", "Max. tijd tractorvermogen", "Rijden", unit="min", hi=250, default=250)
P("drive.parkBrake", "Parkeerrem", "Rijden", "bool", default=0)

for _k, _g in (("rearHitch", "Achterhef"), ("frontHitch", "Fronthef")):
    P(f"{_k}.position", "Positie", _g, unit="%", hi=100, step=0.4, default=80 if _k == "rearHitch" else 100)
    P(f"{_k}.inWork", "Werkstand", _g, "select", default=-1, options=INWORK)
    P(f"{_k}.threshold", "Drempel werkstand", _g, unit="%", hi=100, default=30, hint="in werkstand als de positie lager is")
    P(f"{_k}.draft", "Trekkracht", _g, unit="N", lo=-100000, hi=200000, step=100, default=12000 if _k == "rearHitch" else 0)
    P(f"{_k}.linkForce", "Nominale onderstangkracht", _g, unit="%", lo=-100, hi=100, step=0.8, default=20 if _k == "rearHitch" else 0)
    P(f"{_k}.limit", "Begrenzing", _g, "select", default=0, options=ST_LIMIT)
    P(f"{_k}.rate", "Snelheid bij commando", _g, unit="%/s", lo=1, hi=100, default=25, hint="hoe snel de hef een commando van het werktuig volgt")

for _k, _g in (("rearPto", "Aftakas achter"), ("frontPto", "Aftakas voor")):
    P(f"{_k}.engaged", "Ingeschakeld", _g, "bool", default=0)
    P(f"{_k}.speed", "Toerental (setpoint)", _g, unit="rpm", hi=1300, step=5, default=540 if _k == "rearPto" else 1000)
    P(f"{_k}.mode1000", "Modus", _g, "select", default=0, options=[[0, "540"], [1, "1000"], [3, "n.b."]])
    P(f"{_k}.economy", "Economy", _g, "select", default=0, options=[[0, "uit"], [1, "aan"], [3, "n.b."]])
    P(f"{_k}.limit", "Begrenzing", _g, "select", default=0, options=ST_LIMIT)
    P(f"{_k}.ramp", "Aanloop", _g, unit="rpm/s", lo=10, hi=5000, step=10, default=400, hint="actueel toerental volgt het setpoint met deze snelheid")

P("valves.count", "Aantal ventielen", "Hydrauliek", lo=0, hi=16, default=4)
for _i in range(16):
    P(f"valve{_i}.state", f"Ventiel {_i + 1}", "Hydrauliek", "select", default=0,
      options=[[0, "geblokkeerd"], [1, "uitschuiven"], [2, "intrekken"], [3, "zweven"]])
    P(f"valve{_i}.flow", f"Debiet {_i + 1}", "Hydrauliek", unit="%", hi=100, default=50)
    P(f"valve{_i}.failsafe", f"Fail-safe {_i + 1}", "Hydrauliek", "bool", default=0)

LIGHTS = [("lowBeam", "Dimlicht"), ("highBeam", "Grootlicht"), ("leftTurn", "Richtingaanwijzer links"),
          ("rightTurn", "Richtingaanwijzer rechts"), ("beacon", "Zwaailicht"), ("brake", "Remlichten"),
          ("marker", "Breedtelichten"), ("frontWork", "Werklampen voor"), ("rearWork", "Werklampen achter"),
          ("implWork", "Werklampen werktuig"), ("reverse", "Achteruitrijlicht")]
for _k, _l in LIGHTS:
    P(f"lights.{_k}", _l, "Verlichting", "bool", default=1 if _k == "marker" else 0)

P("gnss.lat", "Breedtegraad", "GNSS", unit="°", lo=-90, hi=90, step=0.000001, default=51.0275)
P("gnss.lon", "Lengtegraad", "GNSS", unit="°", lo=-180, hi=180, step=0.000001, default=4.4813)
P("gnss.alt", "Hoogte", "GNSS", unit="m", lo=-100, hi=3000, step=0.1, default=12)
P("gnss.heading", "Koers", "GNSS", unit="°", hi=359.9, step=0.1, default=90)
P("gnss.turnRate", "Draaisnelheid", "GNSS", unit="°/s", lo=-20, hi=20, step=0.1, default=0, hint="≠ 0: de tractor rijdt in een cirkel")
P("gnss.move", "Positie laten meebewegen", "GNSS", "bool", default=1, hint="positie volgt uit grondsnelheid en koers")
P("gnss.fix", "Fix", "GNSS", "select", default=4,
  options=[[0, "geen fix"], [1, "GNSS"], [2, "DGNSS"], [3, "precise GNSS"], [4, "RTK fixed"], [5, "RTK float"], [6, "geschat (DR)"], [8, "gesimuleerd"]])
P("gnss.sats", "Satellieten", "GNSS", hi=60, default=16)
P("gnss.hdop", "HDOP", "GNSS", hi=50, step=0.1, default=0.8)
P("gnss.pdop", "PDOP", "GNSS", hi=50, step=0.1, default=1.4)
P("gnss.pitch", "Helling (pitch)", "GNSS", unit="°", lo=-45, hi=45, step=0.1, default=0)
P("gnss.roll", "Zijhelling (roll)", "GNSS", unit="°", lo=-45, hi=45, step=0.1, default=0)

P("sys.stop", "Stopschakelaar werktuigen", "Systeem", "select", default=1, options=[[1, "werktuigen toegestaan"], [0, "STOP alle werktuigen"], [3, "n.b."]])
P("sys.language", "Taal", "Systeem", "text", default="nl", hint="2 letters (ISO 639)")
P("sys.country", "Land", "Systeem", "text", default="BE", hint="2 letters (ISO 3166)")
P("sys.decimal", "Decimaalteken", "Systeem", "select", default=0, options=[[0, "komma"], [1, "punt"]])
P("sys.timeFormat", "Tijdnotatie", "Systeem", "select", default=0, options=[[0, "24 uur"], [1, "12 uur"]])
P("sys.dateFormat", "Datumnotatie", "Systeem", "select", default=0,
  options=[[0, "ddmmjjjj"], [1, "ddjjjjmm"], [2, "mmjjjjdd"], [3, "mmddjjjj"], [4, "jjjjmmdd"], [5, "jjjjddmm"]])
P("sys.units", "Eenheden", "Systeem", "select", default=0, options=[[0, "metrisch"], [1, "imperiaal"], [2, "US"]])
P("sys.noise", "Ruis op meetwaarden", "Systeem", "bool", default=0, hint="kleine variatie op toerental, snelheid en aftakas")

P("cmd.hitch", "Hefcommando's volgen", "Werktuigcommando's", "bool", default=0, hint="TECU klasse 3: Hitch and PTO Commands (PGN 65090)")
P("cmd.pto", "Aftakascommando's volgen", "Werktuigcommando's", "bool", default=0)
P("cmd.valves", "Ventielcommando's volgen", "Werktuigcommando's", "bool", default=0, hint="Aux Valve Command (PGN 65072–65087)")
P("cmd.speed", "Snelheidscommando's volgen", "Werktuigcommando's", "bool", default=0, hint="Machine Selected Speed Command (PGN 64835)")

PARAM_BY_KEY = {p["key"]: p for p in PARAMS}
DEFAULTS = {p["key"]: p["default"] for p in PARAMS}


# ------------------------------------------------------------------ encoding helpers
def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def r8(v, scale=1.0, off=0.0):
    return _clamp(int(round((v - off) / scale)), 0, 0xFA)


def r16(v, scale=1.0, off=0.0):
    return _clamp(int(round((v - off) / scale)), 0, 0xFAFF)


def r32(v, scale=1.0, off=0.0):
    return _clamp(int(round((v - off) / scale)), 0, 0xFAFFFFFF)


def b16(v):
    return struct.pack("<H", v)


def b32(v):
    return struct.pack("<I", v)


def bits2(*vals):
    """Packs 2-bit fields, first value in bits 1-2 (least significant)."""
    out = 0
    for i, v in enumerate(vals):
        out |= (int(v) & 3) << (2 * i)
    return out


# ------------------------------------------------------------------ messages
class Sim:
    """State + dynamics of the tractor. `p` holds the settable values, `live` the computed ones."""

    def __init__(self, params=None):
        self.p = dict(DEFAULTS)
        if params:
            self.p.update({k: v for k, v in params.items() if k in PARAM_BY_KEY})
        self.live = {"wheelDist": 0.0, "groundDist": 0.0, "rearPto": 0.0, "frontPto": 0.0,
                     "lat": self.p["gnss.lat"], "lon": self.p["gnss.lon"], "heading": self.p["gnss.heading"]}
        self.sid = 0
        self.stop_count = 0
        self.targets = {}  # from implement commands: key → (target, timestamp)
        self.autopilot = None  # task controller: drives over the field in lanes (tc/field.py Autopilot)

    def get(self, key):
        v = self.p[key]
        if self.p["sys.noise"] and key in ("engine.rpm", "drive.speed", "engine.torque"):
            v = v * (1 + random.uniform(-0.004, 0.004))
        return v

    def set(self, key, value):
        meta = PARAM_BY_KEY.get(key)
        if not meta:
            return False
        if meta["kind"] == "text":
            value = str(value)[:2]
        elif meta["kind"] == "bool":
            value = 1 if value in (True, 1, "1", "true", "on") else 0
        else:
            value = float(value)
            if meta["kind"] == "select" or float(value).is_integer():
                value = int(value) if float(value).is_integer() else value
            if meta["kind"] == "num":
                value = _clamp(value, meta["min"], meta["max"])
        old = self.p.get(key)
        if key == "sys.stop" and old != value:
            self.stop_count = (self.stop_count + 1) % 251
        if key in ("gnss.lat", "gnss.lon", "gnss.heading"):
            self.live[{"gnss.lat": "lat", "gnss.lon": "lon", "gnss.heading": "heading"}[key]] = value
        self.p[key] = value
        return True

    @property
    def ground_speed(self):
        return max(0.0, self.get("drive.speed")) / 3.6  # m/s

    @property
    def wheel_speed(self):
        return self.ground_speed * (1 + self.p["drive.slip"] / 100.0)

    def step(self, dt):
        """Dynamics: distances, PTO run-up, engine hours, position, implement commands. → changed keys."""
        changed = set()
        self.live["groundDist"] += self.ground_speed * dt
        self.live["wheelDist"] += self.wheel_speed * dt
        if self.p["engine.rpm"] > 300:
            self.p["engine.hours"] += dt / 3600.0
        for k in ("rearPto", "frontPto"):
            target = self.p[f"{k}.speed"] if self.p[f"{k}.engaged"] else 0.0
            cur = self.live[k]
            step = self.p[f"{k}.ramp"] * dt
            self.live[k] = target if abs(target - cur) <= step else cur + math.copysign(step, target - cur)
        if self.autopilot is not None:
            self.autopilot.step(self, dt)
        elif self.p["gnss.turnRate"]:
            self.live["heading"] = (self.live["heading"] + self.p["gnss.turnRate"] * dt) % 360.0
        if self.autopilot is None and self.p["gnss.move"] and self.ground_speed > 0:
            d = self.ground_speed * dt * (1 if self.p["drive.direction"] != 0 else -1)
            h = math.radians(self.live["heading"])
            self.live["lat"] += d * math.cos(h) / 111320.0
            self.live["lon"] += d * math.sin(h) / (111320.0 * max(0.01, math.cos(math.radians(self.live["lat"]))))
        now = time.monotonic()
        for key, (target, t) in list(self.targets.items()):
            if now - t > 2.0:  # no more commands: stop following
                del self.targets[key]
                continue
            cur = self.p[key]
            if key.endswith(".position"):
                step = self.p[key.split(".")[0] + ".rate"] * dt
            elif key == "drive.speed":
                step = 3.0 * dt
            else:
                step = abs(target - cur)
            new = target if abs(target - cur) <= step else cur + math.copysign(step, target - cur)
            if new != cur:
                self.p[key] = round(new, 3)
                changed.add(key)
        return changed

    # ----- message encoders (8 bytes, or a list of (pgn, bytes) for message groups)
    def eec1(self):
        rpm, tq, dtq = self.get("engine.rpm"), self.get("engine.torque"), self.p["engine.demandTorque"]
        return bytes([0xF0, r8(dtq, 1, -125), r8(tq, 1, -125)]) + b16(r16(rpm, 0.125)) + bytes([0xFF, 0xF0, r8(dtq, 1, -125)])

    def etc2(self):
        g = self.p["drive.gear"]
        return bytes([r8(g, 1, -125)]) + b"\xff\xff" + bytes([r8(g, 1, -125)]) + b"\xff\xff\xff\xff"

    def _speed_msg(self, speed, dist, b6, b7):
        return b16(r16(speed, 0.001)) + b32(r32(dist % 4211081.0, 0.001)) + bytes([b6, b7])

    def wheel_based(self):
        p = self.p
        b7 = bits2(p["drive.direction"], p["drive.key"], p["drive.startStop"], p["drive.reversed"])
        return self._speed_msg(self.wheel_speed, self.live["wheelDist"], r8(p["drive.maxPowerTime"]), b7)

    def ground_based(self):
        return self._speed_msg(self.ground_speed, self.live["groundDist"], 0xFF, 0xFC | (self.p["drive.direction"] & 3))

    def machine_selected(self):
        p = self.p
        src = p["drive.source"]
        speed = self.wheel_speed if src == 0 else self.ground_speed
        b7 = (p["drive.direction"] & 3) | ((src & 7) << 2) | ((p["drive.speedLimit"] & 7) << 5)
        return self._speed_msg(speed, self.live["groundDist"], 0xFF, b7)

    def hitch(self, k):
        p = self.p
        pos = p[f"{k}.position"]
        iw = p[f"{k}.inWork"]
        if iw == -1:
            iw = 1 if pos < p[f"{k}.threshold"] else 0
        b1 = 0x07 | ((p[f"{k}.limit"] & 7) << 3) | ((iw & 3) << 6)
        return bytes([r8(pos, 0.4), b1, r8(p[f"{k}.linkForce"], 0.8, -100)]) + b16(r16(p[f"{k}.draft"], 10, -320000)) + b"\xff\xff\xff"

    def pto(self, k):
        p = self.p
        actual = self.live[k]
        if self.p["sys.noise"] and actual:
            actual *= 1 + random.uniform(-0.003, 0.003)
        b4 = bits2(3, p[f"{k}.economy"], p[f"{k}.mode1000"], 1 if p[f"{k}.engaged"] else 0)
        b5 = 0x01 | ((p[f"{k}.limit"] & 7) << 1) | (3 << 4) | (3 << 6)
        return b16(r16(actual, 0.125)) + b16(r16(p[f"{k}.speed"], 0.125)) + bytes([b4, b5, 0xFF, 0xFF])

    def valve_flow(self, i):
        st, flow = self.p[f"valve{i}.state"], self.p[f"valve{i}.flow"]
        ext = flow if st == 1 else -flow if st == 2 else 0
        b2 = (st & 0x0F) | 0x30 | ((1 if self.p[f"valve{i}.failsafe"] else 0) << 6)
        return bytes([r8(ext, 1, -125), r8(-ext, 1, -125), b2, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])

    def valves(self, base=0xFE10):
        return [(base + i, self.valve_flow(i)) for i in range(int(self.p["valves.count"]))]

    def lighting(self):
        L = lambda k: 1 if self.p[f"lights.{k}"] else 0
        blink = int(time.time() * 1.5) % 2
        return bytes([
            bits2(3, 0, L("lowBeam"), L("highBeam")),  # daytime running, alternate, low beam, high beam
            bits2(0, L("beacon"), L("rightTurn") & blink, L("leftTurn") & blink),  # front fog, beacon, right, left
            bits2(L("reverse"), L("brake"), L("brake"), L("brake")),  # back-up light, centre, right, left stop
            bits2(L("marker"), L("marker"), L("marker"), L("marker")),  # implement/tractor clearance and marker
            bits2(0, 0, L("rearWork"), L("rearWork")),  # rear fog, underside, rear low, rear high work lights
            bits2(0, 0, L("frontWork"), L("frontWork")),  # side low/high, front low/high work lights
            bits2(0, 0, L("implWork"), L("implWork")),  # OEM 2/1, implement right/left forward work lights
            bits2(0, L("implWork"), L("implWork"), L("implWork")),  # data request, right/left facing, rear work lights
        ])

    def stop_switch(self):
        return b"\xff" * 6 + bytes([self.stop_count, 0xFC | (self.p["sys.stop"] & 3)])

    def language(self):
        p = self.p
        lang = (str(p["sys.language"]) + "  ")[:2].encode("ascii", "replace")
        country = (str(p["sys.country"]) + "  ")[:2].encode("ascii", "replace")
        u = p["sys.units"] & 3
        return lang + bytes([0x0F | ((p["sys.timeFormat"] & 3) << 4) | ((p["sys.decimal"] & 3) << 6),
                             p["sys.dateFormat"], u | (u << 2) | (u << 4) | (u << 6), u | (u << 2) | (u << 4) | (u << 6)]) + country

    def time_date(self):
        t = datetime.datetime.now(datetime.timezone.utc)
        off = datetime.datetime.now().astimezone().utcoffset() or datetime.timedelta()
        mins = int(off.total_seconds() // 60)
        return bytes([min(250, int(t.second * 4)), t.minute, t.hour, t.month, t.day * 4, _clamp(t.year - 1985, 0, 250),
                      125 + int(math.copysign(abs(mins) % 60, mins)), 125 + int(mins / 60)])

    def et1(self):
        return bytes([r8(self.p["engine.coolant"], 1, -40), 0xFF]) + b16(r16(self.p["engine.oilTemp"], 0.03125, -273)) + b"\xff\xff\xff\xff"

    def eflp1(self):
        return bytes([0xFF, 0xFF, 0xFF, r8(self.p["engine.oilPressure"], 4), 0xFF, 0xFF, 0xFF, 0xFF])

    def lfe1(self):
        return b16(r16(self.p["engine.fuelRate"], 0.05)) + b"\xff\xff\xff\xff" + bytes([r8(self.p["engine.throttle"], 0.4), 0xFF])

    def vep1(self):
        v = b16(r16(self.p["engine.battery"], 0.05))
        return b"\xff\xff" + v + v + v

    def dd1(self):
        return bytes([0xFF, r8(self.p["engine.fuelLevel"], 0.4)]) + b"\xff" * 6

    def amb(self):
        return bytes([r8(self.p["env.baro"], 0.5), 0xFF, 0xFF]) + b16(r16(self.p["env.ambient"], 0.03125, -273)) + b"\xff\xff\xff"

    def hours(self):
        return b32(r32(self.p["engine.hours"], 0.05)) + b"\xff\xff\xff\xff"

    def ccvs1(self):
        b0 = 0xF3 | ((1 if self.p["drive.parkBrake"] else 0) << 2)
        return bytes([b0]) + b16(r16(self.ground_speed * 3.6, 1 / 256)) + b"\xff" * 5

    def vehicle_position(self):
        return b32(r32(self.live["lat"], 1e-7, -210)) + b32(r32(self.live["lon"], 1e-7, -210))

    def vehicle_dir_speed(self):
        return (b16(r16(self.live["heading"], 1 / 128)) + b16(r16(self.ground_speed * 3.6, 1 / 256))
                + b16(r16(self.p["gnss.pitch"], 1 / 128, -200)) + b16(r16(self.p["gnss.alt"], 0.125, -2500)))

    def n2k_position(self):
        return struct.pack("<ii", int(round(self.live["lat"] * 1e7)), int(round(self.live["lon"] * 1e7)))

    def n2k_cog_sog(self):
        cog = int(round(math.radians(self.live["heading"]) * 1e4)) & 0xFFFF
        return bytes([self.sid, 0xFC]) + b16(cog) + b16(int(round(self.ground_speed * 100)) & 0xFFFF) + b"\xff\xff"

    def n2k_attitude(self):
        yaw = int(round(math.radians(self.live["heading"] if self.live["heading"] <= 180 else self.live["heading"] - 360) * 1e4))
        return struct.pack("<Bhhh", self.sid, yaw, int(round(math.radians(self.p["gnss.pitch"]) * 1e4)),
                           int(round(math.radians(self.p["gnss.roll"]) * 1e4))) + b"\xff"

    def n2k_gnss(self):
        """PGN 129029 GNSS Position Data (43 bytes, sent as fast packet)."""
        now = time.time()
        days = int(now // 86400)
        secs = int(round((now - days * 86400) * 1e4))
        fix = self.p["gnss.fix"]
        return (struct.pack("<BHI", self.sid, days, secs)
                + struct.pack("<qqq", int(round(self.live["lat"] * 1e16)), int(round(self.live["lon"] * 1e16)),
                              int(round(self.p["gnss.alt"] * 1e6)))
                + bytes([0x00 | ((fix & 0x0F) << 4), 0xFC, int(self.p["gnss.sats"])])
                + struct.pack("<hhiB", int(round(self.p["gnss.hdop"] * 100)), int(round(self.p["gnss.pdop"] * 100)), 0, 0))


# key, name, pgn, priority, interval ms (0 = only on request), control function, default on, encoder, note
MESSAGES = [
    ("eec1", "EEC1 motortoerental en koppel", 0xF004, 3, 100, "tecu", True, lambda s: s.eec1(), ""),
    ("wbs", "Wheel-based Speed and Distance", 0xFE48, 3, 100, "tecu", True, lambda s: s.wheel_based(), ""),
    ("gbs", "Ground-based Speed and Distance", 0xFE49, 3, 100, "tecu", True, lambda s: s.ground_based(), ""),
    ("mss", "Machine Selected Speed", 0xF022, 3, 100, "tecu", True, lambda s: s.machine_selected(), ""),
    ("rhitch", "Rear Hitch Status", 0xFE45, 3, 100, "tecu", True, lambda s: s.hitch("rearHitch"), ""),
    ("fhitch", "Front Hitch Status", 0xFE46, 3, 100, "tecu", True, lambda s: s.hitch("frontHitch"), ""),
    ("rpto", "Rear PTO Output Shaft", 0xFE43, 3, 100, "tecu", True, lambda s: s.pto("rearPto"), ""),
    ("fpto", "Front PTO Output Shaft", 0xFE44, 3, 100, "tecu", True, lambda s: s.pto("frontPto"), ""),
    ("valves", "Aux Valve Estimated Flow (per ventiel)", 0xFE10, 3, 100, "tecu", True, lambda s: s.valves(0xFE10),
     "bitposities ventielstatus niet geverifieerd"),
    ("valvesm", "Aux Valve Measured Flow (per ventiel)", 0xFE20, 3, 100, "tecu", False, lambda s: s.valves(0xFE20),
     "niet geverifieerd"),
    ("stop", "All Implements Stop Operations Switch", 0xFD02, 3, 100, "tecu", True, lambda s: s.stop_switch(), "niet geverifieerd"),
    ("lights", "Lighting Command", 0xFE41, 3, 1000, "tecu", True, lambda s: s.lighting(), "volgorde lampbits niet geverifieerd"),
    ("lang", "Language Command", 0xFE0F, 6, 0, "tecu", True, lambda s: s.language(), "bij opstart en op aanvraag"),
    ("td", "Time/Date", 0xFEE6, 6, 1000, "tecu", True, lambda s: s.time_date(), ""),
    ("etc2", "ETC2 versnelling", 0xF005, 6, 100, "tecu", True, lambda s: s.etc2(), "J1939"),
    ("ccvs1", "CCVS1 snelheid en parkeerrem", 0xFEF1, 6, 100, "tecu", True, lambda s: s.ccvs1(), "J1939"),
    ("et1", "ET1 motortemperatuur", 0xFEEE, 6, 1000, "tecu", True, lambda s: s.et1(), "J1939"),
    ("eflp1", "EFL/P1 oliedruk", 0xFEEF, 6, 500, "tecu", True, lambda s: s.eflp1(), "J1939"),
    ("lfe1", "LFE1 brandstofverbruik", 0xFEF2, 6, 100, "tecu", True, lambda s: s.lfe1(), "J1939"),
    ("vep1", "VEP1 accuspanning", 0xFEF7, 6, 1000, "tecu", True, lambda s: s.vep1(), "J1939"),
    ("dd1", "DD1 brandstofniveau", 0xFEFC, 6, 1000, "tecu", True, lambda s: s.dd1(), "J1939"),
    ("amb", "AMB omgevingstemperatuur", 0xFEF5, 6, 1000, "tecu", True, lambda s: s.amb(), "J1939"),
    ("hours", "Engine Hours", 0xFEE5, 6, 0, "tecu", True, lambda s: s.hours(), "J1939, op aanvraag"),
    ("vpos", "Vehicle Position", 0xFEF3, 6, 1000, "gnss", True, lambda s: s.vehicle_position(), ""),
    ("vds", "Vehicle Direction/Speed", 0xFEE8, 6, 1000, "gnss", True, lambda s: s.vehicle_dir_speed(), ""),
    ("n2kpos", "NMEA 2000 Position Rapid Update (129025)", 0x1F801, 2, 100, "gnss", True, lambda s: s.n2k_position(), ""),
    ("n2kcog", "NMEA 2000 COG & SOG Rapid Update (129026)", 0x1F802, 2, 250, "gnss", True, lambda s: s.n2k_cog_sog(), ""),
    ("n2kgnss", "NMEA 2000 GNSS Position Data (129029)", 0x1F805, 3, 1000, "gnss", True, lambda s: s.n2k_gnss(), "fast packet"),
    ("n2katt", "NMEA 2000 Attitude (127257)", 0x1F119, 3, 1000, "gnss", True, lambda s: s.n2k_attitude(), ""),
]
FAST_PACKET = {0x1F805}
MSG_BY_KEY = {m[0]: m for m in MESSAGES}


# ------------------------------------------------------------------ messages from implements
def _st(v):
    return {0: "uit", 1: "aan", 2: "fout", 3: "n.b."}[v & 3]


def describe_rx(pgn, d):
    """Text for a message an implement sends to the tractor (layouts marked ? are unverified)."""
    if pgn == 0xFE42:
        return (f"fronthef {'n.b.' if d[0] > 250 else f'{d[0] * 0.4:.1f} %'}, achterhef {'n.b.' if d[1] > 250 else f'{d[1] * 0.4:.1f} %'}, "
                f"PTO voor {'n.b.' if d[3] >= 0xFB else f'{(d[2] | d[3] << 8) * 0.125:.0f} rpm'} {_st(d[6])}, "
                f"PTO achter {'n.b.' if d[5] >= 0xFB else f'{(d[4] | d[5] << 8) * 0.125:.0f} rpm'} {_st(d[6] >> 2)} (?)")
    if 0xFE30 <= pgn <= 0xFE3F:
        st = {0: "blokkeren", 1: "uitschuiven", 2: "intrekken", 3: "zweven"}.get(d[2] & 0x0F, f"status {d[2] & 0x0F}")
        return f"ventiel {pgn - 0xFE30 + 1}: debiet {'n.b.' if d[0] > 250 else f'{d[0] * 0.4:.0f} %'}, {st} (?)"
    if pgn == 0xFD43:
        sp = d[0] | d[1] << 8
        return f"snelheid {'n.b.' if sp >= 0xFB00 else f'{sp * 0.0036:.2f} km/h'}, richting {_st(d[7])} (?)"
    if pgn == 0xFE47:
        return f"maintain power: ECU-voeding {_st(d[0] >> 6)}, actuatorvoeding {_st(d[0] >> 4)} (?)"
    if pgn == 0xFE0D:
        return f"Working Set Master, {d[0]} leden"
    return ""


RX_NAMES = {0xFE42: "Hitch and PTO Commands", 0xFD43: "Machine Selected Speed Command", 0xFE47: "Maintain Power",
            0xFE40: "Lighting Data", 0xAD00: "Guidance System Command", 0xFE0D: "Working Set Master"}
for _i in range(16):
    RX_NAMES[0xFE30 + _i] = f"Aux Valve {_i} Command"


class Tractor:
    """Sends the messages of `Sim` on the TECU and GNSS control functions and handles commands."""

    def __init__(self, net, cfs, sim, msg_cfg, events):
        self.net, self.cfs, self.sim, self.msg_cfg, self.ev = net, cfs, sim, msg_cfg, events
        self.counts = {m[0]: 0 for m in MESSAGES}
        self.last = {}
        self.rx = {}  # (pgn, sa) → {...}
        self.fp_seq = 0
        self._tasks = []
        net.listeners.append(self._on_bus)
        for m in MESSAGES:
            cf = cfs.get(m[5])
            if cf is None:
                continue
            if m[0] in ("valves", "valvesm"):
                for i in range(16):
                    cf.requests[m[2] + i] = (lambda i, base: lambda msg: self.sim.valve_flow(i)
                                             if i < self.sim.p["valves.count"] else None)(i, m[2])
            elif m[2] not in FAST_PACKET:
                cf.requests[m[2]] = (lambda enc: lambda msg: enc(self.sim))(m[7])
        for cf in cfs.values():
            cf.on_claimed.append(lambda cf=cf: self._claimed(cf))

    def cfg(self, key):
        m = MSG_BY_KEY[key]
        c = self.msg_cfg.get(key, {})
        return c.get("enabled", m[6]), int(c.get("interval", m[4]))

    def _claimed(self, cf):
        if cf is self.cfs.get("tecu"):
            self.send("lang")

    def start(self):
        self._tasks = [asyncio.ensure_future(self._scheduler()), asyncio.ensure_future(self._dynamics())]

    def stop(self):
        for t in self._tasks:
            t.cancel()
        if self._on_bus in self.net.listeners:
            self.net.listeners.remove(self._on_bus)

    async def _dynamics(self):
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            changed = self.sim.step(now - last)
            last = now
            if changed:
                self.ev.params_changed(changed)

    async def _scheduler(self):
        due = {}
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            self.sim.sid = int(now) & 0xFF
            for m in MESSAGES:
                enabled, interval = self.cfg(m[0])
                if not enabled or interval <= 0:
                    continue
                t = due.get(m[0])
                if t is None or now - t > 5 * interval / 1000:
                    t = now + random.uniform(0, interval / 1000)  # spread the messages over the interval
                if now >= t:
                    self.send(m[0])
                    t += interval / 1000
                due[m[0]] = t

    def send(self, key):
        m = MSG_BY_KEY[key]
        cf = self.cfs.get(m[5])
        if cf is None or not cf.active:
            return
        try:
            data = m[7](self.sim)
        except Exception as e:  # never stop the scheduler because of one message
            self.last[key] = f"fout: {e}"
            return
        if isinstance(data, list):
            for pgn, d in data:
                cf.send(pgn, d, prio=m[3])
            self.last[key] = " | ".join(d.hex(" ") for _, d in data[:2]) + (" …" if len(data) > 2 else "")
        elif m[2] in FAST_PACKET:
            self.send_fast(cf, m[2], data, m[3])
            self.last[key] = data.hex(" ")
        else:
            cf.send(m[2], data, prio=m[3])
            self.last[key] = data.hex(" ")
        self.counts[key] += 1

    def send_fast(self, cf, pgn, data, prio):
        """NMEA 2000 fast packet: frame 0 = sequence/0, length, 6 bytes; then sequence/index + 7 bytes."""
        self.fp_seq = (self.fp_seq + 1) & 7
        s = self.fp_seq << 5
        frames = [bytes([s, len(data)]) + data[:6]]
        rest = data[6:]
        i = 1
        while rest:
            frames.append(bytes([s | i]) + rest[:7])
            rest = rest[7:]
            i += 1
        for f in frames:
            self.net.raw_send(make_id(prio, pgn, cf.address), f.ljust(8, b"\xff"))

    # ----- commands from implements (TECU class 3)
    def _on_bus(self, msg):
        if msg.pgn not in RX_NAMES or self.net.cf_at(msg.sa):
            return
        d = msg.data.ljust(8, b"\xff")
        self.rx[(msg.pgn, msg.sa)] = {"pgn": msg.pgn, "sa": msg.sa, "name": RX_NAMES[msg.pgn], "data": d.hex(" "),
                                      "text": describe_rx(msg.pgn, d), "t": msg.t, "count": self.rx.get((msg.pgn, msg.sa), {}).get("count", 0) + 1}
        self.ev.rx_changed()
        p, now = self.sim.p, time.monotonic()
        T = self.sim.targets
        if msg.pgn == 0xFE42:
            if p["cmd.hitch"]:
                if d[0] <= 250:
                    T["frontHitch.position"] = (d[0] * 0.4, now)
                if d[1] <= 250:
                    T["rearHitch.position"] = (d[1] * 0.4, now)
            if p["cmd.pto"]:
                changed = set()
                for k, lo, eng, mode in (("frontPto", 2, d[6] & 3, (d[6] >> 4) & 3), ("rearPto", 4, (d[6] >> 2) & 3, (d[6] >> 6) & 3)):
                    sp = d[lo] | d[lo + 1] << 8
                    if sp < 0xFB00:
                        p[f"{k}.speed"] = sp * 0.125
                        changed.add(f"{k}.speed")
                    if eng < 2:
                        p[f"{k}.engaged"] = eng
                        changed.add(f"{k}.engaged")
                    if mode < 2:
                        p[f"{k}.mode1000"] = mode
                        changed.add(f"{k}.mode1000")
                self.ev.params_changed(changed)
        elif 0xFE30 <= msg.pgn <= 0xFE3F and p["cmd.valves"]:
            i = msg.pgn - 0xFE30
            if d[0] <= 250:
                p[f"valve{i}.flow"] = round(d[0] * 0.4)
            if d[2] & 0x0F <= 3:
                p[f"valve{i}.state"] = d[2] & 0x0F
            self.ev.params_changed({f"valve{i}.flow", f"valve{i}.state"})
        elif msg.pgn == 0xFD43 and p["cmd.speed"]:
            sp = d[0] | d[1] << 8
            if sp < 0xFB00:
                T["drive.speed"] = (sp * 0.0036, now)
            if d[7] & 3 < 2:
                p["drive.direction"] = d[7] & 3
                self.ev.params_changed({"drive.direction"})

    def status(self):
        out = []
        for m in MESSAGES:
            enabled, interval = self.cfg(m[0])
            cf = self.cfs.get(m[5])
            out.append({"key": m[0], "name": m[1], "pgn": m[2], "prio": m[3], "interval": interval, "enabled": enabled,
                        "cf": m[5], "count": self.counts[m[0]], "data": self.last.get(m[0], ""), "note": m[8],
                        "sa": cf.address if cf else None})
        return out
