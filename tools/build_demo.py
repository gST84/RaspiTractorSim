#!/usr/bin/env python3
"""Builds the online version: the simulator runs in the browser (Pyodide), no Pi needed. On the page Instellingen
it runs either on a simulated bus with the demo implement (default) or on a real bus through a USB or serial CAN
adapter of the computer (WebUSB / WebSerial, web/can/simbus.js).

    python3 tools/build_demo.py [OUT]              # default: dist/demo (GitHub Pages: .github/workflows/pages.yml)
    python3 tools/build_demo.py --fragment OUT     # page without <html>/<head>/<body>, for a host that adds them
    python3 tools/build_demo.py --analyzer OUT     # only the CAN analyzer (page Variabelen), no Pyodide
    python3 -m http.server -d dist/demo 8000       # try it: http://localhost:8000 (analyzer: /analyzer/)

OUT gets index.html (CSS inlined), the web modules, pysrc.json (the Python
sources) and pyodide/ (Pyodide core, downloaded once from the npm CDN into .demo-cache/). Every file is loaded
from the same origin, so the page also works where only same-origin requests are allowed.
OUT/analyzer/ gets the light analyzer: the page Variabelen alone, with USB/serial adapters (WebUSB, WebSerial)
and log files, without the simulator. WebUSB and WebSerial need https or localhost (GitHub Pages is https).
"""
import base64
import json
import os
import re
import shutil
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYODIDE = "0.26.4"
PYODIDE_URL = f"https://cdn.jsdelivr.net/npm/pyodide@{PYODIDE}/"
PYODIDE_FILES = ["pyodide.mjs", "pyodide.asm.js", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json"]
PY_SOURCES = ["tractorsim", "tools/fake_implement.py", "tools/demo_pool.py"]

BOOT = """
<div id="boot" role="status" aria-live="polite">
  <div class="boot-card">
    <b>raspISO wordt gestart</b>
    <p id="boot-step">Python laden (ongeveer 12 MB, de eerste keer)…</p>
    <p class="muted" id="boot-what">Tractor-ECU, virtuele terminal, GNSS en een testwerktuig draaien hier in je browser op een
    gesimuleerde CAN-bus. Op een Raspberry Pi met CAN-HAT praat dezelfde code met een echt werktuig.</p>
  </div>
</div>
<script type="module">
const step = (t) => { document.getElementById('boot-step').textContent = t; };
try {
  // demo bus or a real bus through the CAN adapter of this computer (page Instellingen, stored in this browser)
  const { simBus } = await import('./can/simbus.js');
  const onBus = !simBus.cfg.demo;
  if (onBus) {
    document.getElementById('boot-what').textContent = 'Tractor-ECU, virtuele terminal en GNSS draaien hier in je browser '
      + 'en praten via je CAN-adapter met een echte bus. Terug naar de demo: pagina Instellingen.';
    const badge = document.querySelector('.demo-badge');
    if (badge) { badge.textContent = 'online'; badge.title = 'De simulator draait in deze browser, op een echte CAN-bus via je adapter'; }
  }
  const { loadPyodide } = await import('./pyodide/pyodide.mjs');
  // the standard library archive comes as base64 text; Pyodide gets it from a stand-in for fetch
  const b64 = await (await fetch('./pyodide/python_stdlib.b64.txt')).text();
  const zip = Uint8Array.from(atob(b64.trim()), (c) => c.charCodeAt(0));
  const realFetch = globalThis.fetch;
  globalThis.fetch = (url, opts) => String(url).endsWith('/python_stdlib.zip') ? Promise.resolve(new Response(zip)) : realFetch(url, opts);
  let py;
  try {
    py = await loadPyodide({ indexURL: new URL('./pyodide/', location.href).href });
  } finally {
    globalThis.fetch = realFetch;
  }
  step('Simulator starten…');
  const src = await (await fetch('pysrc.json')).json();
  for (const [path, text] of Object.entries(src)) {
    const full = '/app/' + path;
    py.FS.mkdirTree(full.slice(0, full.lastIndexOf('/')));
    py.FS.writeFile(full, text);
  }
  // the settings of the simulator (config.json) stay in this browser
  let saved = null;
  try { saved = localStorage.getItem('tractorsim.config'); } catch { /* no storage */ }
  if (saved) { py.FS.mkdirTree('/data'); py.FS.writeFile('/data/config.json', saved); }
  py.globals.set('store_config', (text) => { try { localStorage.setItem('tractorsim.config', text); } catch { /* no storage */ } });
  py.globals.set('can_send', onBus ? (id, ext, hex) => simBus.send(id, ext, hex) : null);
  // the waits of the simulator run on our own clock: Chrome slows down the timers of a hidden or covered page,
  // so every event of the CAN adapter also lets the clock tick (web/can/clock.js, tractorsim/browser.py use_clock)
  const { Clock } = await import('./can/clock.js');
  const clock = new Clock();
  globalThis.tractorsimClock = clock; // for diagnosis in the console
  simBus.onActivity = () => clock.kick();
  py.globals.set('clock_schedule', (cb, ms) => clock.schedule(cb, ms));
  let deliver = null;
  py.globals.set('post', (text) => { if (deliver) deliver(text); });
  const demo = await py.runPythonAsync(`
import logging, sys
sys.path[:0] = ['/app', '/app/tools']
logging.basicConfig(level=logging.WARNING)
from tractorsim.browser import Demo, use_clock
use_clock(clock_schedule)
demo = Demo(post, can_send=can_send, store_config=store_config)
await demo.start()
demo`);
  if (onBus) simBus.attach({ receive: (text) => demo.bus_rx(text), state: (up, label) => demo.bus_state(up, label) });
  globalThis.tractorsimBridge = {
    attach(fn) { deliver = fn; demo.hello(); },
    send(text) { demo.receive(text); },
  };
  await import('./app.js');
  document.getElementById('boot').hidden = true;
  if (onBus && !(await simBus.connect(false))) {
    const { toast } = await import('./util.js');
    toast('Geen CAN-adapter verbonden: kies er een op de pagina Instellingen (Verbinden).', true);
  }
} catch (e) {
  console.error(e);
  step('Starten mislukt: ' + (e && e.message ? e.message : e));
  document.getElementById('boot').classList.add('failed');
}
</script>
"""

DEMO_CSS = """
/* demo: no server, so no downloads; start-up screen */
.api-dl { display: none !important; }
.top { top: env(safe-area-inset-top, 0px); }
body::before { content: ""; position: fixed; inset: 0 0 auto; height: env(safe-area-inset-top, 0px); z-index: 30; background: var(--top); }
.demo-badge { font-size: 11px; letter-spacing: .04em; text-transform: uppercase; padding: 2px 7px; border-radius: 999px;
  background: var(--accent-soft); color: var(--accent); white-space: nowrap; }
@media (max-width: 760px) { .demo-badge { display: none; } }
#boot { position: fixed; inset: 0; z-index: 50; display: grid; place-items: center; background: var(--bg);
  padding: max(16px, env(safe-area-inset-top, 0px)) max(16px, env(safe-area-inset-right, 0px))
           max(16px, env(safe-area-inset-bottom, 0px)) max(16px, env(safe-area-inset-left, 0px)); }
#boot .boot-card { max-width: 30rem; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 20px 22px; line-height: 1.5; }
#boot .boot-card b { font-size: 16px; }
#boot .boot-card p { margin: 8px 0 0; }
#boot:not(.failed) #boot-step::after { content: ""; display: inline-block; width: .8em; height: .8em; margin-left: .5em;
  border: 2px solid var(--accent); border-right-color: transparent; border-radius: 50%; vertical-align: -1px;
  animation: spin .8s linear infinite; }
#boot.failed #boot-step { color: var(--err); }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { #boot-step::after { animation: none; } }
"""


def fetch_pyodide():
    cache = os.path.join(ROOT, ".demo-cache", f"pyodide-{PYODIDE}")
    os.makedirs(cache, exist_ok=True)
    for name in PYODIDE_FILES:
        path = os.path.join(cache, name)
        if not os.path.exists(path):
            print(f"downloaden: {PYODIDE_URL}{name}")
            with urllib.request.urlopen(PYODIDE_URL + name) as r, open(path + ".part", "wb") as fh:
                shutil.copyfileobj(r, fh)
            os.replace(path + ".part", path)
    return cache


def theme_css(css):
    """The app follows the system theme; the demo page also follows an explicit data-theme on <html>."""
    m = re.search(r"@media \(prefers-color-scheme: dark\) \{\s*:root \{(.*?)\}\s*\}", css, re.S)
    if not m:
        raise SystemExit("donker thema niet gevonden in style.css")
    body = m.group(1)
    dark = '@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"]) {' + body + "}\n}\n"
    dark += ':root[data-theme="dark"] {' + body + "}\n"
    return css[:m.start()] + dark + css[m.end():]


def page(out_css, fragment=False):
    html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
    body = re.sub(r'<script type="module" src="app.js"></script>\s*', "", body)
    body = body.replace('raspISO</a>', 'raspISO</a>\n  <span class="demo-badge" title="Alles draait in deze '
                        'browser op een gesimuleerde CAN-bus, met een testwerktuig (pagina Instellingen: echte bus)">demo</span>', 1)
    head = ('<title>raspISO</title>\n<meta name="theme-color" content="#1f2a22">\n'
            f"<style>\n{out_css}\n{DEMO_CSS}</style>\n")
    if fragment:
        return head + body.strip() + "\n" + BOOT
    return ('<!doctype html>\n<html lang="nl">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            '<link rel="icon" href="icon.svg">\n' + head + "</head>\n<body>\n" + body.strip() + "\n" + BOOT + "</body>\n</html>\n")


ANALYZER_CSS = """
.analyzer-badge { font-size: 11px; letter-spacing: .04em; text-transform: uppercase; padding: 2px 7px; border-radius: 999px;
  background: var(--accent-soft); color: var(--accent); white-space: nowrap; }
"""


def analyzer_page(out_css):
    html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
    body = body.replace('raspISO</a>', 'raspISO</a>\n  <span class="analyzer-badge" title="Enkel de CAN-analyzer: '
                        'USB- of seriële adapter, of een logbestand">analyzer</span>', 1)
    return ('<!doctype html>\n<html lang="nl">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            '<meta name="theme-color" content="#1f2a22">\n<link rel="icon" href="icon.svg">\n'
            '<title>raspISO Analyzer</title>\n'
            f"<style>\n{out_css}\n{ANALYZER_CSS}</style>\n</head>\n<body data-mode=\"analyzer\">\n{body.strip()}\n</body>\n</html>\n")


def build_analyzer(out):
    """The CAN analyzer alone (no Pyodide, about 300 kB): app.js shows only the page Variabelen."""
    if os.path.isdir(out):
        shutil.rmtree(out)
    shutil.copytree(os.path.join(ROOT, "web"), out, ignore=shutil.ignore_patterns("index.html", "style.css", ".DS_Store"))
    css = theme_css(open(os.path.join(ROOT, "web", "style.css"), encoding="utf-8").read())
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(analyzer_page(css))
    files = sorted(os.path.relpath(os.path.join(d, f), out) for d, _, fs in os.walk(out) for f in fs)
    total = sum(os.path.getsize(os.path.join(out, f)) for f in files)
    print(f"analyzer: {len(files)} bestanden, {total / 1e3:.0f} kB in {out}")
    return files


def python_sources():
    src = {}
    for item in PY_SOURCES:
        path = os.path.join(ROOT, item)
        files = [path] if os.path.isfile(path) else [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs]
        for f in sorted(files):
            if f.endswith(".py") and "__pycache__" not in f:
                src[os.path.relpath(f, ROOT).replace(os.sep, "/")] = open(f, encoding="utf-8").read()
    return src


def build(out, fragment=False):
    cache = fetch_pyodide()
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "pyodide"))
    shutil.copytree(os.path.join(ROOT, "web"), out, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("index.html", "style.css", ".DS_Store"))
    for name in PYODIDE_FILES:
        if name == "python_stdlib.zip":  # .zip is not a type every host serves: ship it as base64 text
            with open(os.path.join(cache, name), "rb") as src, open(os.path.join(out, "pyodide", "python_stdlib.b64.txt"), "w") as dst:
                dst.write(base64.b64encode(src.read()).decode("ascii"))
        else:
            shutil.copy(os.path.join(cache, name), os.path.join(out, "pyodide", name))
    css = theme_css(open(os.path.join(ROOT, "web", "style.css"), encoding="utf-8").read())
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page(css, fragment))
    with open(os.path.join(out, "pysrc.json"), "w", encoding="utf-8") as fh:
        json.dump(python_sources(), fh)
    files = sorted(os.path.relpath(os.path.join(d, f), out) for d, _, fs in os.walk(out) for f in fs)
    total = sum(os.path.getsize(os.path.join(out, f)) for f in files)
    print(f"{len(files)} bestanden, {total / 1e6:.1f} MB in {out}")
    if not fragment:
        build_analyzer(os.path.join(out, "analyzer"))
    return files


if __name__ == "__main__":
    flags = {"--fragment", "--analyzer"}
    args = [a for a in sys.argv[1:] if a not in flags]
    if "--analyzer" in sys.argv[1:]:
        build_analyzer(os.path.abspath(args[0] if args else os.path.join(ROOT, "dist", "analyzer")))
    else:
        build(os.path.abspath(args[0] if args else os.path.join(ROOT, "dist", "demo")), fragment="--fragment" in sys.argv[1:])
