# RaspiTractorSim

Een ISOBUS-tractor op een Raspberry Pi met CAN-HAT. De Pi doet zich voor als **tractor-ECU**, **virtuele terminal (VT)**
en **GNSS-ontvanger**. Via een webpagina stel je alle tractorwaarden in en bedien je de VT waarop het werktuig zijn
scherm (object pool) toont.

```
 werktuig ──ISOBUS 250 kbit/s── CAN-HAT ── Raspberry Pi ── wifi/ethernet ── browser (pc, tablet, gsm)
                                            ├ Tractor-ECU   adres F0
                                            ├ VT            adres 26
                                            └ GNSS          adres 1C
```

Geen externe Python-pakketten: SocketCAN, de webserver en de WebSocket komen uit de standaardbibliotheek
(Python 3.9 of nieuwer). Getest zonder hardware op de virtuele bus; nog **niet getest op een Pi met een echt werktuig**.

## Pagina's

| Pagina | Wat |
|---|---|
| **Tractor** | Alle gesimuleerde waarden als schuifregelaar, knop of keuzelijst; live tegels; scenario's (stilstand, transport, veldwerk, kopakker); tabel van verzonden berichten (aan/uit, interval, laatste data, "Zend"); wat werktuigen naar de tractor sturen. |
| **Virtuele terminal** | Het scherm van de VT: werksetknoppen links, actief data- of alarmmasker, softkeys rechts (met paginering als er meer zijn dan fysieke toetsen). Klik/tik op softkeys, knoppen en invoervelden; berichtenlog; pool downloaden als `.iop`; een `.iop` openen zonder werktuig (bv. uit PLC-VT-Designer). |
| **Bus** | Busbelasting en foutframes, alle knooppunten met gedecodeerde NAME, live CAN-log met filter, CSV-export, een frame of request zelf versturen. |
| **Instellingen** | Per ECU: aan/uit, adres, NAME. VT: versie, schermgrootte, softkeys, strenge poolcontrole. |

## Snel proberen zonder hardware (ook op een Mac of pc)

```bash
python3 -m tractorsim --bus virtual --port 8080
```

Open http://localhost:8080 en start in een tweede venster het testwerktuig:

```bash
python3 tools/fake_implement.py
```

Het testwerktuig claimt adres 0x80, vraagt de VT-gegevens op, uploadt een pool van 17 kB via ETP (of laadt een opgeslagen
versie), toont toerental, snelheid, aftakas en hef van de gesimuleerde tractor en reageert op softkeys, knoppen en
invoervelden. "Hef op/neer" stuurt Hitch and PTO Commands: zet op de Tractor-pagina **Hefcommando's volgen** aan en de
hef beweegt mee. Bij "Spuiten aan" en meer dan 15 km/h verschijnt een alarmmasker.

`python3 tools/fake_implement.py --pool mijn.iop` uploadt je eigen pool.

## Installatie op de Raspberry Pi

### 1. CAN-HAT aansluiten

De meeste CAN-HAT's hebben een MCP2515. Zet de overlay in `/boot/firmware/config.txt` (oudere systemen: `/boot/config.txt`)
en herstart. Kristalfrequentie en interruptpin staan op de HAT of in de handleiding:

```
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
```

(Waveshare RS485 CAN HAT: `oscillator=12000000`; PiCAN2: `16000000`, pin 25.) Controleer na herstarten met `ip link show can0`.

Bekabeling: CAN_H, CAN_L en massa naar de ISOBUS. Een bus heeft aan beide uiteinden een afsluiting van 120 Ω nodig;
op een testbank met één werktuig volstaan twee weerstanden van 120 Ω (of de jumper op de HAT plus die van het werktuig/de kabel).

### 2. Programma installeren

```bash
git clone https://github.com/gST84/RaspiTractorSim.git ~/RaspiTractorSim
cd ~/RaspiTractorSim
python3 -m unittest discover -s tests
sudo deploy/install.sh can0 8080
```

De service zet `can0` op 250 kbit/s (met automatische herstart na bus-off en een grotere zendwachtrij) en start de
simulator bij elke boot. Webinterface: `http://<adres-van-de-pi>:8080`. Logboek: `journalctl -u tractorsim -f`.

Met de hand (zonder service):

```bash
sudo ip link set can0 type can bitrate 250000 restart-ms 100
sudo ip link set can0 txqueuelen 1000
sudo ip link set can0 up
python3 -m tractorsim --bus can0
```

Instellingen en opgeslagen pools staan in `~/.tractorsim` (`config.json`, `versions/<NAME>/`).

### Testen op de Pi zonder werktuig

```bash
sudo modprobe vcan && sudo ip link add vcan0 type vcan && sudo ip link set vcan0 up
python3 -m tractorsim --bus vcan0 &
python3 tools/fake_implement.py --bus vcan0
```

### Op een echte tractor

Zet op de pagina **Instellingen** de Tractor-ECU en de GNSS-ontvanger uit: anders zenden twee TECU's tegenstrijdige
waarden. De VT kan blijven: kies een vrij adres en functie-instantie 1 als er al een terminal op de bus zit.

## Wat de tractor-ECU zendt

Alle waarden zijn instelbaar op de Tractor-pagina; per bericht kun je het interval wijzigen of het uitzetten.
Op een Request antwoordt de TECU met het gevraagde bericht, anders met een NACK.

| PGN | Bericht | Interval | Layout |
|---|---|---|---|
| F004 | EEC1 toerental, koppel | 100 ms | gecontroleerd met `decode.py` |
| FE48 / FE49 | Wheel- / Ground-based Speed and Distance | 100 ms | gecontroleerd met `decode.py` |
| F022 | Machine Selected Speed | 100 ms | gecontroleerd met `decode.py` |
| FE45 / FE46 | Rear / Front Hitch Status | 100 ms | gecontroleerd met `decode.py` |
| FE43 / FE44 | Rear / Front PTO | 100 ms | volgens `ISO_TractorDecode.scl` |
| FE10–FE1F | Aux Valve Estimated Flow (per ventiel) | 100 ms | statusbits niet geverifieerd |
| FE20–FE2F | Aux Valve Measured Flow | uit | niet geverifieerd |
| FD02 | All Implements Stop Operations Switch | 100 ms | niet geverifieerd |
| FE41 | Lighting Command | 1 s | volgorde lampbits niet geverifieerd |
| FE0F | Language Command | opstart + aanvraag | ook door de VT |
| FEE6 | Time/Date | 1 s | |
| F005, FEF1, FEEE, FEEF, FEF2, FEF7, FEFC, FEF5, FEE5 | J1939: versnelling, parkeerrem, temperaturen, oliedruk, verbruik, accu, brandstofniveau, buitentemperatuur, motoruren | 0,1–1 s | J1939-71 |
| FEF3 / FEE8 | Vehicle Position, Direction/Speed (GNSS-adres) | 1 s | |
| 1F801 / 1F802 / 1F805 / 1F119 | NMEA 2000 positie, koers/snelheid, GNSS Position Data (fast packet), attitude | 0,1–1 s | 1F805 zoals `decode.py` |

De positie beweegt mee met grondsnelheid en koers; met een draaisnelheid rijdt de tractor in een cirkel.

**Van werktuigen** (TECU klasse 3, per onderdeel aan te zetten): Hitch and PTO Commands (FE42), Aux Valve Command (FE30–FE3F)
en Machine Selected Speed Command (FD43). De hef volgt een commando met de ingestelde snelheid; stoppen de commando's,
dan blijft de laatste waarde staan. Deze drie layouts zijn **niet geverifieerd**: de ruwe bytes staan erbij in de tabel
"Ontvangen van werktuigen", vergelijk ze met een log van een echt werktuig.

## Wat de VT kan

- Verbinden: Working Set Maintenance (time-out 3 s), Get Memory, Get Number of Soft Keys, Get Text Font Data, Get Hardware,
  Get Supported Widechars, Get Window Mask Data, Get Supported Objects, VT Status elke seconde.
- Pool ontvangen via TP en ETP (ook meerdere Object Pool Transfers en latere aanvullingen), parsen en controleren; bij een
  fout antwoordt End of Object Pool met het foute object. Store / Load / Delete Version / Get Versions: pools worden per NAME
  op de SD-kaart bewaard.
- Objecttypes 0–28 tekenen (zelfde tekencode als PLC-VT-Designer) plus Output List; v4-types 34–40 worden geparset.
- Commando's: Hide/Show, Enable/Disable, Select Input Object, ESC, Control Audio Signal (piep in de browser), Set Audio Volume,
  Change Child Location/Position, Size, Background Colour, Numeric Value, String Value, End Point, Font/Line/Fill Attributes,
  Active Mask, Soft Key Mask, Attribute, Priority, List Item, Object Label, Polygon Point/Scale, Get Attribute Value,
  Execute (Extended) Macro, Lock/Unlock Mask, Delete Object Pool, Identify VT. Macro's lopen ook op events
  (On Key Press/Release, On Show/Hide, On Change Value, …).
- Bediening: Soft Key / Button Activation (ingedrukt, vastgehouden elke 200 ms, losgelaten, afgebroken bij maskerwissel),
  vergrendelbare knoppen, invoer van getallen (met schaal/offset/min/max), lijsten, ja/nee en tekst (met de toegelaten tekens
  van de Input Attributes), Pointing Event (v4).
- Alarmmaskers met prioriteit hoog/middel brengen hun werkset naar voren.

Niet ondersteund: auxiliary control (AUX-N), Graphics Context, Colour Map, Window Masks/Key Groups tekenen (user layout),
extended versions (v5), proprietaire fonts. De foutbits in de antwoorden zijn vereenvoudigd en de AID-nummering voor
Change Attribute volgt de veldvolgorde van de objecten (niet tegen de norm gecontroleerd).

## Bestanden

| Pad | Inhoud |
|---|---|
| `tractorsim/canbus.py` | SocketCAN (stdlib) en de virtuele UDP-bus |
| `tractorsim/j1939.py` | identifiers, NAME, address claim, requests, TP/BAM/ETP |
| `tractorsim/tecu.py` | instelbare waarden, berichtencoders, dynamiek, werktuigcommando's |
| `tractorsim/vt/objects.py` | object pool: definities, parser, encoder, controle |
| `tractorsim/vt/server.py` | de virtuele terminal |
| `tractorsim/app.py`, `web.py` | samenhang, configuratie, HTTP + WebSocket |
| `web/` | de webinterface; `web/vt/render.js` is afgeleid van `src/render.js` van PLC-VT-Designer |
| `tools/fake_implement.py`, `tools/demo_pool.py` | testwerktuig en zijn object pool |
| `tests/test_all.py` | `python3 -m unittest discover -s tests` |
| `deploy/` | systemd-service en installatiescript |
