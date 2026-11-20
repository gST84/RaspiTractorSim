#!/usr/bin/env python3
"""Builds the online demo: the simulator and the demo implement run in the browser (Pyodide), no Pi needed.

    python3 tools/build_demo.py [OUT]          # default: dist/demo
    python3 -m http.server -d dist/demo 8000   # try it: http://localhost:8000

OUT gets index.html (page without <html>/<head>/<body>, CSS inlined), the web modules, pysrc.json (the Python
sources) and pyodide/ (Pyodide core, downloaded once from the npm CDN into .demo-cache/). Every file is loaded
from the same origin, so the page also works where only same-origin requests are allowed.
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
    <b>RaspiTractorSim wordt gestart</b>
    <p id="boot-step">Python laden (ongeveer 12 MB, de eerste keer)…</p>
    <p class="muted">Tractor-ECU, virtuele terminal, GNSS en een testwerktuig draaien hier in je browser op een
    gesimuleerde CAN-bus. Op een Raspberry Pi met CAN-HAT praat dezelfde code met een echt werktuig.</p>
  </div>
</div>
<script type="module">
const step = (t) => { document.getElementById('boot-step').textContent = t; };
try {
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
  let deliver = null;
  py.globals.set('post', (text) => { if (deliver) deliver(text); });
  const demo = await py.runPythonAsync(`
import logging, sys
sys.path[:0] = ['/app', '/app/tools']
logging.basicConfig(level=logging.WARNING)
from tractorsim.browser import Demo
demo = Demo(post)
await demo.start()
demo`);
  globalThis.tractorsimBridge = {
    attach(fn) { deliver = fn; demo.hello(); },
    send(text) { demo.receive(text); },
  };
  await import('./app.js');
  document.getElementById('boot').hidden = true;
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


def page(out_css):
    html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
    body = re.sub(r'<script type="module" src="app.js"></script>\s*', "", body)
    body = body.replace('RaspiTractorSim</a>', 'RaspiTractorSim</a>\n  <span class="demo-badge" title="Alles draait in deze '
                        'browser op een gesimuleerde CAN-bus, met een testwerktuig">demo</span>', 1)
    head = ('<title>RaspiTractorSim</title>\n<meta name="theme-color" content="#1f2a22">\n'
            f"<style>\n{out_css}\n{DEMO_CSS}</style>\n")
    return head + body.strip() + "\n" + BOOT


def python_sources():
    src = {}
    for item in PY_SOURCES:
        path = os.path.join(ROOT, item)
        files = [path] if os.path.isfile(path) else [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs]
        for f in sorted(files):
            if f.endswith(".py") and "__pycache__" not in f:
                src[os.path.relpath(f, ROOT).replace(os.sep, "/")] = open(f, encoding="utf-8").read()
    return src


def build(out):
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
        fh.write(page(css))
    with open(os.path.join(out, "pysrc.json"), "w", encoding="utf-8") as fh:
        json.dump(python_sources(), fh)
    files = sorted(os.path.relpath(os.path.join(d, f), out) for d, _, fs in os.walk(out) for f in fs)
    total = sum(os.path.getsize(os.path.join(out, f)) for f in files)
    print(f"{len(files)} bestanden, {total / 1e6:.1f} MB in {out}")
    return files


if __name__ == "__main__":
    build(os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist", "demo")))
