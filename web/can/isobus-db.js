// Built-in ISOBUS (ISO 11783-7) / SAE J1939 / NMEA 2000 messages as signal definitions (see signals.js).
//
// Sources, the same as the rest of raspISO and PLC-VT-Designer:
//   - the status messages of the tractor: ISO_TractorDecode (checked against a real tractor log) and tecu.py;
//   - commands from implements (Hitch and PTO Commands, Aux Valve Command, Tractor Facilities, Machine Selected
//     Speed Command, Maintain Power): ISO 11783-7:2002 annex B and AgIsoStack++, as in ISO_TractorEcu;
//   - NMEA 2000: AgIsoStack++ (position, COG/SOG, GNSS position data, attitude, DOPs).
// Fields marked `note: 'niet geverifieerd'` follow the sources above but were not checked on a bus.

import { FAST_PACKET } from './transport.js';

// ---------------------------------------------------------------- helpers (byte and bit are 0-based)
const S = (name, byte, bit, len, scale = 1, offset = 0, unit = '', x = {}) =>
  ({ name, start: byte * 8 + bit, len, order: 'le', signed: false, scale, offset, unit, na: 'j1939', ...x });
const E = (name, byte, bit, len, values, x = {}) => S(name, byte, bit, len, 1, 0, '', { type: 'enum', values, ...x });
const B = (name, byte, bit) => S(name, byte, bit, 1, 1, 0, '', { type: 'enum', values: { 0: 'nee', 1: 'ja' }, na: null });
const T = (name, byte, n) => ({ name, start: byte * 8, len: n * 8, order: 'le', type: 'text' });
const N = (name, byte, len, scale, unit, x = {}) => // NMEA 2000 little-endian field
  ({ name, start: byte * 8, len, order: 'le', signed: !!x.signed, scale, offset: 0, unit, na: 'n2k', ...x });

const ST = { 0: 'uit', 1: 'aan', 2: 'fout', 3: 'n.b.' };
const ON = { 0: 'uit', 1: 'aan', 2: 'fout', 3: 'n.b.' };
const DIR = { 0: 'achteruit', 1: 'vooruit', 2: 'fout', 3: 'n.b.' };
const LIMIT = { 0: 'niet begrensd', 1: 'begrensd door bestuurder', 2: 'begrensd hoog', 3: 'begrensd laag', 6: 'onherstelbare fout', 7: 'n.b.' };
const INWORK = { 0: 'uit werkstand', 1: 'in werkstand', 2: 'fout', 3: 'n.b.' };
const SRC = { 0: 'wielsnelheid', 1: 'grondsnelheid', 2: 'navigatie', 3: 'gemengd', 4: 'gesimuleerd', 7: 'n.b.' };
const VALVE = { 0: 'blokkeren', 1: 'uitschuiven', 2: 'intrekken', 3: 'zweven', 14: 'fout', 15: 'n.b.' };
const REQ = { 0: 'uit', 1: 'aan', 2: 'fout', 3: 'geen actie' };
const TORQUE_MODE = { 0: 'lage stationair', 1: 'acceleratorpedaal', 2: 'cruise control', 3: 'PTO-regelaar', 4: 'snelheidsregelaar',
  5: 'ASR', 6: 'transmissie', 7: 'ABS', 8: 'koppelbegrenzing', 9: 'hoge stationair', 10: 'remsysteem', 11: 'remote accelerator', 15: 'n.b.' };
const FIX = { 0: 'geen fix', 1: 'GNSS-fix', 2: 'DGNSS', 3: 'precise GNSS', 4: 'RTK fixed', 5: 'RTK float', 6: 'geschat (DR)',
  7: 'handmatig', 8: 'gesimuleerd', 14: 'fout', 15: 'n.b.' };
const DEG = 180 / Math.PI;

const speed = (p, note) => [
  S('Snelheid', 0, 0, 16, 0.001, 0, 'm/s'),
  S('Snelheid (km/h)', 0, 0, 16, 0.0036, 0, 'km/h', { decimals: 2, derived: true }),
  S('Afstand', 2, 0, 32, 0.001, 0, 'm'),
  E('Rijrichting', 7, 0, 2, DIR),
  ...(p === 0xfe48 ? [S('Max. tijd tractorvoeding', 6, 0, 8, 1, 0, 'min'), E('Sleutelschakelaar', 7, 2, 2, ON),
    E('Start/stop', 7, 4, 2, { 0: 'stop', 1: 'start', 2: 'fout', 3: 'n.b.' }), E('Bediening achteruit', 7, 6, 2, ON)] : []),
  ...(p === 0xf022 ? [S('Exit/reason code', 6, 0, 6), E('Snelheidsbron', 7, 2, 3, SRC), E('Begrenzing', 7, 5, 3, LIMIT)] : []),
].map((s) => (note ? { ...s, note } : s));

const hitch = [
  S('Positie', 0, 0, 8, 0.4, 0, '%'),
  E('Begrenzing', 1, 3, 3, LIMIT),
  E('In werkstand', 1, 6, 2, INWORK),
  S('Nominale onderstangkracht', 2, 0, 8, 0.8, -100, '%'),
  S('Trekkracht', 3, 0, 16, 10, -320000, 'N'),
  S('Exit/reason code', 5, 0, 6),
];

const pto = [
  S('Toerental', 0, 0, 16, 0.125, 0, 'rpm'),
  S('Setpunt toerental', 2, 0, 16, 0.125, 0, 'rpm'),
  E('Inschakelverzoek', 4, 0, 2, REQ),
  E('Economy-modus', 4, 2, 2, ON),
  E('1000-modus', 4, 4, 2, ON),
  E('Ingeschakeld', 4, 6, 2, ON),
  E('Begrenzing', 5, 1, 3, LIMIT),
  E('Verzoek economy', 5, 4, 2, REQ),
  E('Verzoek modus', 5, 6, 2, REQ),
  S('Exit/reason code', 6, 0, 6),
];

const LAMP = (byte, names) => names.map((n, i) => (n ? E(n, byte, i * 2, 2, ST) : null)).filter(Boolean);

/** pgn → { name, signals, note? } */
export const MESSAGES = new Map();
const M = (pgn, name, signals = [], note = '') => MESSAGES.set(pgn, { pgn, name, signals, note });

// ---- network management and transport
M(0xee00, 'Address Claimed', [
  S('Identity number', 0, 0, 21, 1, 0, '', { na: null }), S('Manufacturer code', 2, 5, 11, 1, 0, '', { na: null }),
  S('ECU instance', 4, 0, 3, 1, 0, '', { na: null }), S('Function instance', 4, 3, 5, 1, 0, '', { na: null }),
  S('Function', 5, 0, 8, 1, 0, '', { na: null }), S('Device class', 6, 1, 7, 1, 0, '', { na: null }),
  S('Device class instance', 7, 0, 4, 1, 0, '', { na: null }), S('Industry group', 7, 4, 3, 1, 0, '', { na: null }),
  B('Self-configurable', 7, 7), S('NAME', 0, 0, 64, 1, 0, '', { type: 'hex', na: null }),
]);
M(0xea00, 'Request', [S('Gevraagde PGN', 0, 0, 24, 1, 0, '', { type: 'hex', na: null })]);
M(0xe800, 'Acknowledgement', [
  E('Controle', 0, 0, 8, { 0: 'ACK', 1: 'NACK', 2: 'toegang geweigerd', 3: 'kan niet antwoorden (bezig)' }, { na: null }),
  S('Groepsfunctie', 1, 0, 8, 1, 0, '', { na: null }), S('Adres', 4, 0, 8, 1, 0, '', { type: 'hex', na: null }),
  S('PGN', 5, 0, 24, 1, 0, '', { type: 'hex', na: null }),
]);
const TPCTL = { 16: 'RTS', 17: 'CTS', 19: 'EoMA', 32: 'BAM', 255: 'Abort' };
M(0xec00, 'TP.CM (transportbeheer)', [E('Controle', 0, 0, 8, TPCTL, { na: null }), S('Grootte', 1, 0, 16, 1, 0, 'byte', { na: null }),
  S('Pakketten', 3, 0, 8, 1, 0, '', { na: null }), S('PGN', 5, 0, 24, 1, 0, '', { type: 'hex', na: null })]);
M(0xeb00, 'TP.DT (transportdata)', [S('Volgnummer', 0, 0, 8, 1, 0, '', { na: null })]);
M(0xc800, 'ETP.CM (transportbeheer)', [E('Controle', 0, 0, 8, { 20: 'RTS', 21: 'CTS', 22: 'DPO', 23: 'EoMA', 255: 'Abort' }, { na: null }),
  S('PGN', 5, 0, 24, 1, 0, '', { type: 'hex', na: null })]);
M(0xc700, 'ETP.DT (transportdata)', [S('Volgnummer', 0, 0, 8, 1, 0, '', { na: null })]);
M(0xfeda, 'Software Identification');
M(0xfdc5, 'ECU Identification');
M(0xfeca, 'DM1 actieve storingen');
M(0xfecb, 'DM2 vorige storingen');
M(0xfe0d, 'Working Set Master', [S('Aantal leden', 0, 0, 8, 1, 0, '', { na: null })]);
M(0xfe0c, 'Working Set Member', [S('NAME', 0, 0, 64, 1, 0, '', { type: 'hex', na: null })]);
M(0xe600, 'VT → ECU');
M(0xe700, 'ECU → VT');
M(0xcb00, 'Process Data (TC)');
M(0xef00, 'Proprietary A');

// ---- tractor (TECU)
M(0xf004, 'EEC1 motor', [
  E('Koppelmodus', 0, 0, 4, TORQUE_MODE), S('Gevraagd koppel bestuurder', 1, 0, 8, 1, -125, '%'),
  S('Actueel koppel', 2, 0, 8, 1, -125, '%'), S('Motortoerental', 3, 0, 16, 0.125, 0, 'rpm'),
  S('Bronadres regeling', 5, 0, 8, 1, 0, '', { type: 'hex' }), S('Startermodus', 6, 0, 4),
  S('Gevraagd koppel motor', 7, 0, 8, 1, -125, '%'),
]);
M(0xf005, 'ETC2 transmissie', [S('Gekozen versnelling', 0, 0, 8, 1, -125), S('Actuele overbrenging', 1, 0, 16, 0.001),
  S('Huidige versnelling', 3, 0, 8, 1, -125)]);
M(0xfe48, 'Wheel-based Speed and Distance', speed(0xfe48));
M(0xfe49, 'Ground-based Speed and Distance', speed(0xfe49));
M(0xf022, 'Machine Selected Speed', speed(0xf022));
M(0xfe45, 'Rear Hitch Status', hitch);
M(0xfe46, 'Front Hitch Status', hitch);
M(0xfe43, 'Rear PTO Output Shaft', pto);
M(0xfe44, 'Front PTO Output Shaft', pto);
for (let i = 0; i < 16; i++) {
  M(0xfe10 + i, `Aux Valve ${i} Estimated Flow`, [S('Debiet uitschuiven', 0, 0, 8, 1, -125, '%'), S('Debiet intrekken', 1, 0, 8, 1, -125, '%'),
    E('Ventielstatus', 2, 0, 4, VALVE), E('Fail-safe', 2, 6, 2, ON)]);
  M(0xfe20 + i, `Aux Valve ${i} Measured Flow`, [S('Debiet uitschuiven', 0, 0, 8, 1, -125, '%'), S('Debiet intrekken', 1, 0, 8, 1, -125, '%')]);
  M(0xfe30 + i, `Aux Valve ${i} Command`, [S('Gevraagd debiet', 0, 0, 8, 0.4, 0, '%'), E('Gevraagde status', 2, 0, 4, VALVE),
    E('Fail-safe', 2, 6, 2, ON)]);
}
M(0xfe42, 'Hitch and PTO Commands', [
  S('Fronthef positie', 0, 0, 8, 0.4, 0, '%'), S('Achterhef positie', 1, 0, 8, 0.4, 0, '%'),
  S('Aftakas voor toerental', 2, 0, 16, 0.125, 0, 'rpm'), S('Aftakas achter toerental', 4, 0, 16, 0.125, 0, 'rpm'),
  E('Aftakas voor inschakelen', 6, 6, 2, ST), E('Aftakas achter inschakelen', 6, 4, 2, ST),
  E('Aftakas voor 1000-modus', 7, 6, 2, ST), E('Aftakas achter 1000-modus', 7, 4, 2, ST),
  E('Aftakas voor economy', 7, 2, 2, ST), E('Aftakas achter economy', 7, 0, 2, ST),
]);
M(0xfd43, 'Machine Selected Speed Command', [S('Gevraagde snelheid', 0, 0, 16, 0.001, 0, 'm/s'),
  S('Gevraagde snelheid (km/h)', 0, 0, 16, 0.0036, 0, 'km/h', { decimals: 2, derived: true }),
  S('Snelheidslimiet', 2, 0, 16, 0.001, 0, 'm/s'), E('Rijrichting', 7, 0, 2, DIR)]);
M(0xfe47, 'Maintain Power', [E('ECU-voeding gevraagd', 0, 6, 2, ST), E('Actuatorvoeding gevraagd', 0, 4, 2, ST),
  E('Werktuig in werkstand', 1, 0, 2, ST), E('Werktuig in transport', 1, 6, 2, ST)], 'niet geverifieerd');
const CLASS = { 0: 'klasse 1', 1: 'klasse 2', 2: 'klasse 3', 3: 'n.b.' };
const facilities = [
  E('Klasse', 0, 6, 2, CLASS, { na: null }), B('Sleutelschakelaar', 0, 5), B('Max. tijd tractorvoeding', 0, 4),
  B('Maintain power', 0, 3), B('Wielsnelheid', 0, 2), B('Grondsnelheid', 0, 1), B('Motortoerental', 0, 0),
  B('Achterhef positie', 1, 7), B('Achterhef in werkstand', 1, 6), B('Aftakas achter toerental', 1, 5),
  B('Aftakas achter ingeschakeld', 1, 4), B('Verlichting', 1, 3), B('Language Command', 1, 2),
  B('Time/Date', 2, 7), B('Grondafstand', 2, 6), B('Grondrichting', 2, 5), B('Wielafstand', 2, 4), B('Wielrichting', 2, 3),
  B('Achterhef trekkracht', 2, 2), B('Aux-ventielen status', 2, 0),
  B('Opdracht achterhef', 3, 7), B('Opdracht aftakas achter toerental', 3, 6), B('Opdracht aftakas achter inschakelen', 3, 5),
  B('Opdracht aux-ventielen', 3, 4), B('Limit status reporting', 3, 3),
  B('Navigatie: GNSS', 4, 7), B('Navigatie: positie', 4, 6),
  B('Fronthef positie', 5, 7), B('Fronthef in werkstand', 5, 6), B('Aftakas voor toerental', 5, 5), B('Aftakas voor ingeschakeld', 5, 4),
  B('Fronthef trekkracht', 5, 3), B('Opdracht fronthef', 5, 2), B('Opdracht aftakas voor toerental', 5, 1), B('Opdracht aftakas voor inschakelen', 5, 0),
];
M(0xfe09, 'Tractor Facilities Response', facilities);
M(0xfe08, 'Required Tractor Facilities', facilities);
M(0xfd02, 'All Implements Stop Operations Switch State', [S('Teller', 6, 0, 8, 1, 0, '', { na: null }),
  E('Schakelaar', 7, 0, 2, { 0: 'STOP alle werktuigen', 1: 'werktuigen toegestaan', 2: 'fout', 3: 'n.b.' })], 'niet geverifieerd');
const UNIT = { 0: 'metrisch', 1: 'imperiaal/US', 2: 'gereserveerd', 3: 'n.b.' };
M(0xfe0f, 'Language Command', [T('Taal', 0, 2),
  E('Decimaalteken', 2, 6, 2, { 0: 'komma', 1: 'punt', 2: 'gereserveerd', 3: 'n.b.' }),
  E('Tijdnotatie', 2, 4, 2, { 0: '24 uur', 1: '12 uur', 2: 'gereserveerd', 3: 'n.b.' }),
  E('Datumnotatie', 3, 0, 8, { 0: 'ddmmjjjj', 1: 'ddjjjjmm', 2: 'mmjjjjdd', 3: 'mmddjjjj', 4: 'jjjjmmdd', 5: 'jjjjddmm' }),
  E('Afstand', 4, 6, 2, UNIT), E('Oppervlakte', 4, 4, 2, UNIT), E('Volume', 4, 2, 2, UNIT), E('Massa', 4, 0, 2, UNIT),
  E('Temperatuur', 5, 6, 2, UNIT), E('Druk', 5, 4, 2, UNIT), E('Kracht', 5, 2, 2, UNIT), E('Eenhedensysteem', 5, 0, 2, UNIT),
  T('Land', 6, 2)]);
M(0xfee6, 'Time/Date', [S('Seconden', 0, 0, 8, 0.25, 0, 's'), S('Minuten', 1, 0, 8, 1, 0, 'min'), S('Uren', 2, 0, 8, 1, 0, 'h'),
  S('Maand', 3, 0, 8), S('Dag', 4, 0, 8, 0.25), S('Jaar', 5, 0, 8, 1, 1985), S('Lokale verschuiving minuten', 6, 0, 8, 1, -125, 'min'),
  S('Lokale verschuiving uren', 7, 0, 8, 1, -125, 'h')]);
M(0xfe41, 'Lighting Command', [
  ...LAMP(0, ['Dagrijlicht', 'Alternatieve koplampen', 'Dimlicht', 'Grootlicht']),
  ...LAMP(1, ['Mistlampen voor', 'Zwaailicht', 'Richtingaanwijzer rechts', 'Richtingaanwijzer links']),
  ...LAMP(2, ['Achteruitrijlicht', 'Remlicht midden', 'Remlicht rechts', 'Remlicht links']),
  ...LAMP(3, ['Breedtelicht werktuig', 'Breedtelicht tractor', 'Markeringslicht werktuig', 'Markeringslicht tractor']),
  ...LAMP(4, ['Mistlamp achter', 'Werklicht onderzijde', 'Werklicht achter laag', 'Werklicht achter hoog']),
  ...LAMP(5, ['Werklicht zijkant laag', 'Werklicht zijkant hoog', 'Werklicht voor laag', 'Werklicht voor hoog']),
  ...LAMP(6, ['OEM-optie 2', 'OEM-optie 1', 'Werktuig werklicht rechts voor', 'Werktuig werklicht links voor']),
  ...LAMP(7, ['Verzoek lichtdata', 'Werktuig werklicht rechts', 'Werktuig werklicht links', 'Werktuig werklicht achter']),
], 'volgorde lampbits niet geverifieerd');
M(0xfe40, 'Lighting Data');
M(0xac00, 'Guidance Machine Status', [S('Gemeten kromming', 0, 0, 16, 0.25, -8032, '1/km'),
  E('Mechanische vergrendeling', 2, 0, 2, ST), E('Stuurgereedheid', 2, 2, 2, ST), E('Toestemming sturen', 2, 4, 2, ST)], 'niet geverifieerd');
M(0xad00, 'Guidance System Command', [S('Gevraagde kromming', 0, 0, 16, 0.25, -8032, '1/km'),
  E('Stuuropdracht', 2, 0, 2, { 0: 'niet sturen', 1: 'sturen', 2: 'fout', 3: 'n.b.' })], 'niet geverifieerd');

// ---- SAE J1939 engine and vehicle
M(0xfef1, 'CCVS1 snelheid en parkeerrem', [E('Parkeerrem', 0, 2, 2, ON), S('Voertuigsnelheid', 1, 0, 16, 1 / 256, 0, 'km/h', { decimals: 2 })]);
M(0xfeee, 'ET1 motortemperatuur', [S('Koelvloeistof', 0, 0, 8, 1, -40, '°C'), S('Brandstoftemperatuur', 1, 0, 8, 1, -40, '°C'),
  S('Olietemperatuur', 2, 0, 16, 0.03125, -273, '°C')]);
M(0xfeef, 'EFL/P1 oliedruk', [S('Oliedruk', 3, 0, 8, 4, 0, 'kPa')]);
M(0xfef2, 'LFE1 brandstofverbruik', [S('Brandstofverbruik', 0, 0, 16, 0.05, 0, 'l/h'), S('Gaspedaal', 6, 0, 8, 0.4, 0, '%')]);
M(0xfef7, 'VEP1 spanning', [S('Laadspanning', 2, 0, 16, 0.05, 0, 'V'), S('Accuspanning', 4, 0, 16, 0.05, 0, 'V'),
  S('Spanning contactslot', 6, 0, 16, 0.05, 0, 'V')]);
M(0xfefc, 'DD1 brandstofniveau', [S('Brandstofniveau', 1, 0, 8, 0.4, 0, '%')]);
M(0xfef5, 'AMB omgeving', [S('Luchtdruk', 0, 0, 8, 0.5, 0, 'kPa'), S('Omgevingstemperatuur', 3, 0, 16, 0.03125, -273, '°C')]);
M(0xfee5, 'Engine Hours', [S('Draaiuren', 0, 0, 32, 0.05, 0, 'h'), S('Omwentelingen', 4, 0, 32, 1000, 0, 'r')]);
M(0xfef3, 'Vehicle Position', [S('Breedte', 0, 0, 32, 1e-7, -210, '°'), S('Lengte', 4, 0, 32, 1e-7, -210, '°')]);
M(0xfee8, 'Vehicle Direction/Speed', [S('Koers', 0, 0, 16, 1 / 128, 0, '°', { decimals: 2 }), S('Snelheid', 2, 0, 16, 1 / 256, 0, 'km/h', { decimals: 2 }),
  S('Helling', 4, 0, 16, 1 / 128, -200, '°', { decimals: 2 }), S('Hoogte', 6, 0, 16, 0.125, -2500, 'm')]);

// ---- NMEA 2000
const n2k = (pgn) => pgn; // PGNs as numbers, for readability below
M(n2k(129025), 'NMEA 2000 Position Rapid Update', [N('Breedte', 0, 32, 1e-7, '°', { signed: true }), N('Lengte', 4, 32, 1e-7, '°', { signed: true })]);
M(n2k(129026), 'NMEA 2000 COG & SOG Rapid Update', [N('SID', 0, 8, 1, ''), E('Referentie', 1, 0, 2, { 0: 'waar', 1: 'magnetisch', 2: 'fout', 3: 'n.b.' }),
  N('Koers over grond (COG)', 2, 16, 1e-4 * DEG, '°', { decimals: 2 }), N('Snelheid over grond (SOG)', 4, 16, 0.01, 'm/s'),
  N('SOG (km/h)', 4, 16, 0.036, 'km/h', { decimals: 2, derived: true })]);
M(n2k(127257), 'NMEA 2000 Attitude', [N('SID', 0, 8, 1, ''), N('Gieren (yaw)', 1, 16, 1e-4 * DEG, '°', { signed: true, decimals: 2 }),
  N('Stampen (pitch)', 3, 16, 1e-4 * DEG, '°', { signed: true, decimals: 2 }), N('Rollen (roll)', 5, 16, 1e-4 * DEG, '°', { signed: true, decimals: 2 })]);
M(n2k(129029), 'NMEA 2000 GNSS Position Data', [N('SID', 0, 8, 1, ''), N('Dagen sinds 1970', 1, 16, 1, 'd'), N('Tijd van de dag', 3, 32, 1e-4, 's'),
  N('Breedte', 7, 64, 1e-16, '°', { signed: true, decimals: 8 }), N('Lengte', 15, 64, 1e-16, '°', { signed: true, decimals: 8 }),
  N('Hoogte', 23, 64, 1e-6, 'm', { signed: true, decimals: 2 }), E('Systeem', 31, 0, 4, { 0: 'GPS', 1: 'GLONASS', 2: 'GPS+GLONASS', 3: 'GPS+SBAS', 4: 'GPS+SBAS+GLONASS', 5: 'Chayka', 6: 'integrated', 7: 'surveyed', 8: 'Galileo' }, { na: null }),
  E('Methode', 31, 4, 4, FIX, { na: null }), N('Satellieten', 33, 8, 1, ''), N('HDOP', 34, 16, 0.01, '', { signed: true }),
  N('PDOP', 36, 16, 0.01, '', { signed: true }), N('Geoïdescheiding', 38, 32, 0.01, 'm', { signed: true })]);
M(n2k(129539), 'NMEA 2000 GNSS DOPs', [N('SID', 0, 8, 1, ''),
  E('Werkelijke modus', 1, 3, 3, { 0: '1D', 1: '2D', 2: '3D', 3: 'auto', 6: 'fout', 7: 'n.b.' }, { na: null }),
  N('HDOP', 2, 16, 0.01, '', { signed: true }), N('VDOP', 4, 16, 0.01, '', { signed: true }), N('TDOP', 6, 16, 0.01, '', { signed: true })]);
M(n2k(129027), 'NMEA 2000 Position Delta High Precision Rapid Update');
M(n2k(129028), 'NMEA 2000 Altitude Delta High Precision Rapid Update');

/** Definition for a PGN, or a generic one (name only) for the rest. */
export function messageDef(pgn) {
  const m = MESSAGES.get(pgn);
  if (m) return m;
  return { pgn, name: pgnName(pgn), signals: [], generic: true };
}

export function pgnName(pgn) {
  const m = MESSAGES.get(pgn);
  if (m) return m.name;
  if (pgn >= 0xff00 && pgn <= 0xffff) return `Proprietary B (${(pgn & 0xff).toString(16).toUpperCase().padStart(2, '0')})`;
  if (FAST_PACKET.has(pgn) || (pgn >= 0x1f000 && pgn <= 0x1ffff)) return `NMEA 2000 PGN ${pgn}`;
  return `PGN ${pgn}`;
}

// ---------------------------------------------------------------- NAME (ISO 11783-5), as tractorsim/j1939.py
export const NAME_FIELDS = [
  ['identity', 0, 21, 'Identity number'], ['manufacturer', 21, 11, 'Fabrikant'], ['ecuInstance', 32, 3, 'ECU instance'],
  ['functionInstance', 35, 5, 'Function instance'], ['function', 40, 8, 'Functie'], ['reserved', 48, 1, 'Gereserveerd'],
  ['deviceClass', 49, 7, 'Device class'], ['deviceClassInstance', 56, 4, 'Device class instance'],
  ['industryGroup', 60, 3, 'Industry group'], ['selfConfigurable', 63, 1, 'Self-configurable'],
];
export const FUNCTIONS = {
  0: 'Engine', 3: 'Transmission', 7: 'Axle-steering', 16: 'Steering controller', 19: 'Instrument cluster',
  23: 'Vehicle navigation (GNSS)', 25: 'Network interconnect ECU', 28: 'Off-vehicle gateway', 29: 'Virtual Terminal',
  30: 'Management computer', 33: 'System monitor', 61: 'Cab display', 62: 'File server / printer', 128: 'Non-VT display',
  129: 'Operator controls', 130: 'Task Controller', 131: 'Position control', 132: 'Machine control', 133: 'Product rate control',
  134: 'Tractor ECU', 135: 'Section on/off control', 137: 'Product treatment', 139: 'Datalogger', 140: 'Decision support',
  141: 'Lighting controller',
};
export const MANUFACTURERS = {
  1: 'Allied Signal', 69: 'John Deere', 79: 'AGCO', 94: 'CNH Industrial', 97: 'Ag Leader', 98: 'Müller-Elektronik',
  99: 'Fendt', 103: 'Lemken', 111: 'Kverneland', 115: 'Krone', 116: 'Amazone', 135: 'Claas', 148: 'Horsch', 151: 'Raven Industries',
  154: 'Pöttinger',
};
export const INDUSTRY = { 0: 'Global', 1: 'On-highway', 2: 'Landbouw', 3: 'Bouw', 4: 'Maritiem', 5: 'Industrie' };

/** NAME (BigInt) → { identity, manufacturer, … } */
export function nameDecode(n) {
  const out = {};
  for (const [k, pos, bits] of NAME_FIELDS) out[k] = Number((n >> BigInt(pos)) & ((1n << BigInt(bits)) - 1n));
  return out;
}

export function nameFromBytes(d) {
  let n = 0n;
  for (let i = 7; i >= 0; i--) n = (n << 8n) | BigInt(d[i] ?? 0);
  return n;
}

export const functionName = (f) => FUNCTIONS[f] ?? `functie ${f}`;
export const manufacturerName = (m) => MANUFACTURERS[m] ?? `fabrikant ${m}`;

// ---------------------------------------------------------------- DM1 (SAE J1939-73)
export const FMI = {
  0: 'boven normaal (ernstig)', 1: 'onder normaal (ernstig)', 2: 'data onregelmatig/ongeldig', 3: 'spanning te hoog / kortsluiting naar +',
  4: 'spanning te laag / kortsluiting naar massa', 5: 'stroom te laag / open circuit', 6: 'stroom te hoog / kortsluiting',
  7: 'mechanisch systeem reageert niet', 8: 'abnormale frequentie of puls', 9: 'abnormale updatesnelheid', 10: 'abnormale verandering',
  11: 'oorzaak onbekend', 12: 'slecht toestel of component', 13: 'buiten kalibratie', 14: 'speciale instructies',
  15: 'boven normaal (minst ernstig)', 16: 'boven normaal (matig)', 17: 'onder normaal (minst ernstig)', 18: 'onder normaal (matig)',
  19: 'netwerkdata fout', 31: 'conditie aanwezig',
};

/** DM1/DM2 data → { lamps: { mil, red, amber, protect }, codes: [{ spn, fmi, oc, cm }] } */
export function decodeDm(d) {
  const lamp = (b, sh) => ST[(b >> sh) & 3];
  const lamps = { protect: lamp(d[0], 0), amber: lamp(d[0], 2), red: lamp(d[0], 4), mil: lamp(d[0], 6) };
  const codes = [];
  for (let i = 2; i + 3 < d.length; i += 4) {
    const spn = d[i] | (d[i + 1] << 8) | ((d[i + 2] >> 5) << 16);
    const fmi = d[i + 2] & 0x1f;
    if ((spn === 0 && fmi === 0) || (spn === 0x7ffff && fmi === 31)) continue;
    codes.push({ spn, fmi, oc: d[i + 3] & 0x7f, cm: d[i + 3] >> 7 });
  }
  return { lamps, codes };
}

// ---------------------------------------------------------------- VT functions (ISO 11783-6), first byte of E600/E700
export const VT_FUNCTIONS = {
  0x00: 'Soft Key Activation', 0x01: 'Button Activation', 0x02: 'Pointing Event', 0x03: 'VT Select Input Object', 0x04: 'VT ESC',
  0x05: 'VT Change Numeric Value', 0x06: 'VT Change Active Mask', 0x07: 'VT Change Soft Key Mask', 0x08: 'VT Change String Value',
  0x09: 'VT On User-Layout Hide/Show', 0x0a: 'VT Control Audio Signal Termination', 0x11: 'Object Pool Transfer', 0x12: 'End of Object Pool',
  0x20: 'Aux Assignment Type 1', 0x21: 'Aux Input Status Type 1', 0x22: 'Aux Assignment Type 2', 0x23: 'Aux Input Status Type 2',
  0x24: 'Aux Capabilities', 0x25: 'Preferred Assignment', 0x26: 'Aux Input Type 2 Maintenance', 0x27: 'Aux Input Status Type 2 Enable',
  0xa0: 'Hide/Show Object', 0xa1: 'Enable/Disable Object', 0xa2: 'Select Input Object', 0xa3: 'Control Audio Signal', 0xa4: 'Set Audio Volume',
  0xa5: 'Change Child Location', 0xa6: 'Change Size', 0xa7: 'Change Background Colour', 0xa8: 'Change Numeric Value', 0xa9: 'Change End Point',
  0xaa: 'Change Font Attributes', 0xab: 'Change Line Attributes', 0xac: 'Change Fill Attributes', 0xad: 'Change Active Mask',
  0xae: 'Change Soft Key Mask', 0xaf: 'Change Attribute', 0xb0: 'Change Priority', 0xb1: 'Change List Item', 0xb2: 'Delete Object Pool',
  0xb3: 'Change String Value', 0xb4: 'Change Child Position', 0xb5: 'Change Object Label', 0xb6: 'Change Polygon Point',
  0xb7: 'Change Polygon Scale', 0xb8: 'Graphics Context', 0xb9: 'Get Attribute Value', 0xba: 'Select Colour Map', 0xbb: 'Identify VT',
  0xbc: 'Execute Extended Macro', 0xbd: 'Lock/Unlock Mask', 0xbe: 'Execute Macro', 0xc0: 'Get Memory', 0xc1: 'Get Supported Widechars',
  0xc2: 'Get Number of Soft Keys', 0xc3: 'Get Text Font Data', 0xc4: 'Get Window Mask Data', 0xc5: 'Get Supported Objects',
  0xc7: 'Get Hardware', 0xd0: 'Store Version', 0xd1: 'Load Version', 0xd2: 'Delete Version', 0xd3: 'Extended Get Versions',
  0xd4: 'Extended Store Version', 0xd5: 'Extended Load Version', 0xd6: 'Extended Delete Version', 0xdf: 'Get Versions',
  0xe0: 'Get Versions Response', 0xfe: 'VT Status', 0xff: 'Working Set Maintenance',
};
const vtSignals = [E('Functie', 0, 0, 8, VT_FUNCTIONS, { na: null })];
MESSAGES.get(0xe600).signals = vtSignals;
MESSAGES.get(0xe700).signals = vtSignals;
