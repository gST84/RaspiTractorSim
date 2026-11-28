# raspISO

Een ISOBUS-tractor op een Raspberry Pi met CAN-HAT. De Pi doet zich voor als **tractor-ECU**, **virtuele terminal (VT)**,
**GNSS-ontvanger** en **task controller (TC)**. Via een webpagina stel je alle tractorwaarden in, bedien je de VT waarop
het werktuig zijn scherm (object pool) toont en volg je de taak: documentatie, sectiebesturing en taakkaarten.

```
 werktuig ──ISOBUS 250 kbit/s── CAN-HAT ── Raspberry Pi ── wifi/ethernet ── browser (pc, tablet, gsm)
                                            ├ Tractor-ECU   adres F0
                                            ├ VT            adres 26
                                            ├ GNSS          adres 1C
                                            └ TC            adres F7
```

Geen externe Python-pakketten: SocketCAN, de webserver en de WebSocket komen uit de standaardbibliotheek
(Python 3.9 of nieuwer). Getest zonder hardware op de virtuele bus; nog **niet getest op een Pi met een echt werktuig**.

**Online versie: https://gst84.github.io/RaspiTractorSim/**: dezelfde simulator, volledig in de browser (Python via
Pyodide, ongeveer 15 MB bij de eerste keer laden). Standaard is het een demo met een testwerktuig op een gesimuleerde
bus; zet je op **Instellingen** de schakelaar **Demo** uit, dan draait hij op een echte bus via je CAN-adapter (zie
[Online op een echte bus](#online-op-een-echte-bus)). Downloads (pool, CAN-log, instellingen) zijn daar uitgeschakeld;
de instellingen blijven in de browser bewaard. Zelf bouwen: `python3 tools/build_demo.py` (map `dist/demo`).

## Pagina's

| Pagina | Wat |
|---|---|
| **Tractor** | Alle gesimuleerde waarden als schuifregelaar, knop of keuzelijst; live tegels; scenario's (stilstand, transport, veldwerk, kopakker); tabel van verzonden berichten (aan/uit, interval, laatste data, "Zend"); wat werktuigen naar de tractor sturen. |
| **Virtuele terminal** | Het scherm van de VT: werksetknoppen links, actief data- of alarmmasker, softkeys rechts (met paginering als er meer zijn dan fysieke toetsen). Klik/tik op softkeys, knoppen en invoervelden; berichtenlog; pool downloaden als `.iop`; een `.iop` openen zonder werktuig (bv. uit PLC-VT-Designer). |
| **Taakcontroller** | De task controller: aangemelde werktuigen met hun beschrijving (DDOP) en procesdata, taak starten/stoppen, sectiebesturing, taakkaart, perceel en automatisch rijden, een kaart met dekking en dosiszones. Zie [Task controller](#task-controller). |
| **Bus** | Busbelasting en foutframes, alle knooppunten met gedecodeerde NAME, live CAN-log met filter, CSV-export, een frame of request zelf versturen. |
| **Variabelen** | CAN-analyzer: de bus als boom van toestellen, berichten en signalen (zoals een OPC UA-client), attributen, volglijst met trend. Bron: de simulator, een USB- of seriële adapter rechtstreeks in de browser, of een logbestand. Zie [Variabelen](#variabelen-can-analyzer). |
| **Instellingen** | Per ECU: aan/uit, adres, NAME. VT: versie, schermgrootte, softkeys, strenge poolcontrole. TC: versie, aantal booms, secties en kanalen. |

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

**Automatisch bijwerken:** 20 s na het opstarten haalt `tractorsim-update` de laatste versie van GitHub op
(`deploy/update.sh`, `git pull --ff-only`). Alleen bij een nieuwe versie herstart de simulator. Er zijn tot 10 pogingen
voor het netwerk. Zonder internet (bv. op de tractor) start de simulator meteen met de versie die er al is. Met lokale
wijzigingen in de map verandert er niets.

- Nu bijwerken: `sudo systemctl start tractorsim-update`.
- Wat er gebeurde: `journalctl -u tractorsim-update`.
- Uitzetten: `sudo systemctl disable --now tractorsim-update.timer`.
- Na een wijziging in `deploy/` zelf: `sudo deploy/install.sh` opnieuw uitvoeren. Het logboek van de update meldt dat.

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

### Op de Mac met een USB-adapter

macOS heeft geen SocketCAN. Zonder installatie kan het met de [online versie](#online-op-een-echte-bus); met de
server zendt de simulator via een USB-adapter, bijvoorbeeld de Microchip CAN BUS Analyzer:

```bash
brew install libusb
python3 -m venv .venv && .venv/bin/pip install pyusb
.venv/bin/python -m tractorsim --bus mcba
```

- Met meer analyzers: `--bus mcba:1` (volgorde vanaf 0) of `--bus mcba:<serienummer>`.
- De afsluitweerstand blijft zoals hij in de analyzer staat; het logboek toont bij de start of hij aan of uit staat.
- Losgekoppeld? De simulator verbindt opnieuw zodra de analyzer terug is.
- De simulator en de pagina Variabelen (WebUSB) kunnen de analyzer niet tegelijk gebruiken: kies in Variabelen dan
  de bron Simulator.
- Andere adapters via python-can: `pythoncan:pcan:PCAN_USBBUS1` (met PCBUSB van MacCAN), `pythoncan:gs_usb:0`
  (candleLight) of `pythoncan:slcan:/dev/tty.usbmodem…`.

### Online op een echte bus

De online versie kan zelf tractor-ECU, VT en GNSS zijn op een echte bus, zonder installatie: in Chrome of Edge, met
een CAN-adapter in je computer.

1. Zet op **Instellingen** de schakelaar **Demo** uit en klik **Toepassen**: de pagina herstart zonder testwerktuig.
2. Kies de adapter, de bitrate (ISOBUS: 250 kbit/s) en, bij een seriële adapter, de baudrate.
3. Klik **Verbinden…** en kies je adapter. De browser onthoudt hem: zit hij erin, dan verbindt hij bij het openen vanzelf.

| Adapter | Via |
|---|---|
| Microchip CAN BUS Analyzer, candleLight/CANable (gs_usb), PEAK PCAN-USB, PCAN-USB FD | WebUSB |
| CANable met slcan-firmware, USBtin, Lawicel | WebSerial (slcan) |

- De adapter opent normaal (niet listen-only), want de simulator zendt. Zit er al een tractor of terminal op de bus,
  zet dan de TECU uit en kies voor de VT een vrij adres (zie hieronder).
- Houd het tabblad open: de simulator draait in de pagina. Een verborgen of bedekt venster (bv. achter TIA Portal)
  krijgt trage timers van de browser; de simulator tikt dan mee op elk bericht van de adapter (`web/can/clock.js`),
  dus met een PLC op de bus houdt hij zijn ritme (VT Status elke seconde). Zonder ander verkeer valt hij dan stil.
- Op Windows houdt de PEAK-driver een PCAN-USB vast en op Linux nemen `gs_usb`, `peak_usb` en `mcba_usb` de adapter;
  de browser kan er dan niet bij. Gebruik daar de server.
- Het zenden is enkel getest met nagemaakte USB-berichten, nog niet met een echte adapter.

### Op een echte tractor

Zet op de pagina **Instellingen** de Tractor-ECU en de GNSS-ontvanger uit: anders zenden twee TECU's tegenstrijdige
waarden. De VT kan blijven: kies een vrij adres en functie-instantie 1 als er al een terminal op de bus zit. Hetzelfde
voor de task controller: de display van de tractor is meestal zelf een TC. Zonder eigen GNSS neemt de TC de positie
van de bus (Vehicle Position, of NMEA 2000 129025/129026).

Enkel meekijken (pagina **Variabelen**)? Start dan met `--listen`: geen eigen ECU's en de simulator zendt niets,
ook niet via "Zend" op de Bus-pagina. Wie het ook op de interface wil afdwingen: `ip link set can0 type can
bitrate 250000 listen-only on`.

## Variabelen (CAN-analyzer)

De pagina **Variabelen** decodeert alle frames in de browser en toont ze als adresruimte, zoals UaExpert dat doet
voor OPC UA:

```
Bus                      frames per seconde, busbelasting
└ F0 · Tractor ECU       herkend aan zijn NAME (een adreswissel geeft geen nieuwe boom)
   ├ Identiteit          NAME-velden, software (FEDA), ECU-identificatie (FDC5)
   ├ Berichten           per PGN → signalen met waarde, eenheid, status (goed, n.b., fout, verouderd)
   ├ Procesdata          werktuigen: DDOP-elementen → DDI's (actueel en setpunt van de TC)
   └ Diagnose            DM1: lampen en actieve storingen
```

- **Attributen**: pad, ruwe waarde, bitpositie, datatype, schaal, keuzelijst, periode, bron van de definitie.
- **Volglijst**: `+` of dubbelklik op een variabele. Per variabele een kleine trend (30 s tot alles), CSV-export.
  De lijst blijft bewaard in de browser.
- **Opnemen**: alle frames als candump-log (`.log`), terug te openen als logbestand.
- **Decoderen**: ingebouwd voor ISOBUS/J1939 (tractorberichten zoals `ISO_TractorDecode` en `ISO_TractorEcu` van
  PLC-VT-Designer, NAME, DM1, VT-functies, NMEA 2000 met fast packet) met TP, ETP en fast packet.
  - **DBC**: eigen DBC-bestanden gaan voor op de ingebouwde berichten (exact CAN-ID, of de PGN voor `J1939PG`).
  - **TC-procesdata**: de DDOP wordt opgevangen als het werktuig hem naar de TC stuurt. Kwam die niet voorbij, laad
    hem dan als bestand bij het toestel (**DDOP laden…**). Zonder DDOP krijgen DDI's een naam uit een beperkte
    tabel (`web/can/ddi.js`).

### Bronnen

| Bron | Hoe | Opmerking |
|---|---|---|
| Simulator / server | WebSocket (`raw`) | SocketCAN, virtuele bus, Microchip-analyzer (`--bus mcba`), of via python-can: `--bus pythoncan:pcan:PCAN_USBBUS1`, `pythoncan:kvaser:0`, `pythoncan:vector:0`, `pythoncan:ixxat:0` (`pip install python-can` en de driver van de fabrikant) |
| USB-adapter (WebUSB) | driver in de browser | Microchip CAN BUS Analyzer, candleLight/CANable (gs_usb), PEAK PCAN-USB, PCAN-USB FD / Pro FD / X6 |
| Seriële adapter (slcan) | WebSerial | CANable met slcan-firmware, USBtin, Lawicel en compatibele |
| Logbestand | in één keer of afspelen (×1, ×5, ×20) | Microchip `.CAN`, candump, PCAN-View `.trc`, de CSV van de Bus-pagina |

- **Browsers**: WebUSB en WebSerial werken enkel in Chrome en Edge, op `https` of `localhost`. Vanaf een andere pc
  via `http://<pi>:8080` dus geen USB, wel de simulator/server als bron.
- **Drivers van het besturingssysteem**: houdt een driver de adapter vast, dan kan de browser er niet bij.
  - Windows: de PEAK-driver houdt een PCAN-USB vast. Gebruik daar `--bus pythoncan:pcan:…`.
  - Linux: `peak_usb`, `gs_usb` en `mcba_usb` nemen de adapter als SocketCAN-interface. Gebruik daar de server.
  - macOS: geen van deze drivers, dus WebUSB werkt.
- **Enkel luisteren**: de analyzer zendt nooit een frame.
  - slcan opent met `L`, candleLight en PCAN-USB FD in listen-only. PCAN-USB doet dat pas vanaf hardwarerevisie 4.
  - De Microchip-analyzer kent geen listen-only: hij bevestigt frames (ACK), zoals elk toestel op de bus.
- **Getest**:
  - De protocollen volgen de Linux-drivers (`mcba_usb.c`, `gs_usb.c`, `peak_usb/`); de omzetting van USB-berichten
    naar frames zit in de JS-tests.
  - Op echte hardware zijn de USB- en seriële drivers **nog niet getest**.
- **NAME's**: een Address Claimed komt enkel bij het opstarten of op verzoek. Start de analyzer dus vóór het
  contact, of open een log van het begin; de server stuurt de laatste claim per adres mee.

### Analyzer zonder simulator

`python3 tools/build_demo.py --analyzer OUT` maakt een lichte pagina (±250 kB, geen Pyodide) met enkel Variabelen:
USB, serieel en logbestanden. De Pages-build zet die naast de demo onder `/analyzer/`.

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

## Task controller

De TC volgt ISO 11783-10, versie 3 of 4 (in te stellen). Hij meldt in zijn versiebericht documentatie (TC-BAS), taakkaarten
(TC-GEO) en sectiebesturing (TC-SC); de laatste twee kun je op de pagina uitschakelen.

- **Aanmelden:** versie, structuur- en taallabel, de beschrijving (DDOP) via TP of ETP, activeren, wissen, Change Designator.
  De DDOP wordt per NAME bewaard: een werktuig met dezelfde labels activeert hem zonder hem opnieuw te sturen. Een DDOP met
  een fout wordt geweigerd met het foute object. Na 6 s zonder Client Task valt een werktuig weg.
- **Procesdata:** na het activeren stelt de TC de metingen in volgens de triggermethoden van elke DPD (tijd 1 s, bij
  verandering) en vraagt hij elke waarde op. Op de pagina zie je per element de waarde van het werktuig en wat de TC
  stuurde, en zet je een instelbare waarde met de hand.
- **Taak:** start zet de taakbit in de TC Status en Setpoint Work State (DDI 289) op aan; het logboek bewaart elke seconde
  positie, snelheid, secties, dosis en alle procesdata (downloaden als CSV). Totalen tonen de stand en wat er sinds de start
  bij kwam. De dekking is te downloaden als GeoJSON.
- **Sectiebesturing:** de secties komen uit de DDOP (offsets DDI 134/135, breedte DDI 67, ten opzichte van de koppeling);
  de afstand van de GNSS-antenne tot de koppeling stel je op de pagina in. Een sectie gaat uit buiten het perceel of op
  bewerkte grond (instelbare overlap, standaard 50 %), met vooruitkijken. De TC stuurt Setpoint Condensed Work State
  (DDI 290+) zolang het werktuig Section Control State (DDI 160) = automatisch meldt; de werkelijke stand komt uit
  Actual Condensed Work State (DDI 161+). Met de hand: een knop per sectie.
- **Taakkaart:** een GeoJSON (polygonen met een numerieke eigenschap) of een shapefile (.zip met .shp/.dbf/.prj, in WGS84),
  of een demokaart over het perceel. De dosis staat in de eenheid van het werktuig (de weergave van zijn setpunt in de
  DDOP, bv. L/ha); de TC stuurt de dosis op het vooruitkijkpunt naar het gekozen setpunt.
- **Perceel en rijden:** een rechthoek vanaf de huidige positie, of een grens uit een GeoJSON. Met de eigen GNSS rijdt de
  simulator automatisch in banen over een rechthoekig perceel (baanafstand = werkbreedte), met kopakkerbochten buiten
  het perceel.

Het testwerktuig (`tools/fake_implement.py`) is ook een TC-client: een spuit met 8 secties van 3 m
(`tools/demo_tc.py`). Starten met de taak zet het spuiten aan. Getest met dit testwerktuig en in de tests; nog **niet
met TC_Client van PLC-VT-Designer of een echt werktuig**.

Niet in deze versie: peer control, ISO-XML (taakdata en grid-taakkaarten), getrokken werktuigen die anders volgen dan
de tractor, meerdere booms of producten in de sectiebesturing.

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
| `tractorsim/canbus.py` | SocketCAN (stdlib), de virtuele UDP-bus, de Microchip-analyzer via USB (pyusb, optioneel) en python-can (optioneel) |
| `tractorsim/j1939.py` | identifiers, NAME, address claim, requests, TP/BAM/ETP |
| `tractorsim/tecu.py` | instelbare waarden, berichtencoders, dynamiek, werktuigcommando's |
| `tractorsim/vt/objects.py` | object pool: definities, parser, encoder, controle |
| `tractorsim/vt/server.py` | de virtuele terminal |
| `tractorsim/tc/` | de task controller: `server.py` (protocol, taak, logboek), `ddop.py`, `section.py`, `field.py` (perceel, dekking, automatisch rijden), `prescription.py` (taakkaarten) |
| `tractorsim/app.py`, `web.py` | samenhang, configuratie, HTTP + WebSocket |
| `web/` | de webinterface; `web/vt/render.js` is afgeleid van `src/render.js` van PLC-VT-Designer |
| `tools/fake_implement.py`, `tools/demo_pool.py`, `tools/demo_tc.py` | testwerktuig, zijn object pool en zijn TC-client (spuit) |
| `web/tcpage.js`, `web/tc/map.js` | pagina Taakcontroller en de kaart |
| `web/variables.js`, `web/can/` | pagina Variabelen: frames, transport (TP/ETP/fast packet), signalen, ingebouwde database, DBC, DDOP/DDI, model |
| `web/can/sources/` | bronnen: server, WebUSB-drivers (`usb.js`), slcan (`slcan.js`), logbestanden (`logfile.js`) |
| `web/can/simbus.js`, `tractorsim/browser.py` | de online versie: demo of een echte bus via de adapter (WebUSB/WebSerial) |
| `tests/test_all.py` | `python3 -m unittest discover -s tests` |
| `tests/js/` | JS-tests van de analyzer: `jsc -m tests/js/run.mjs` (macOS, JavaScriptCore) of `node tests/js/run.mjs` |
| `deploy/` | systemd-service, installatiescript en het automatisch bijwerken (`update.sh`, `tractorsim-update.service/.timer`) |
