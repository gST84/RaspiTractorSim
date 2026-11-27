import { $, connect, on, onConnection, toast, send } from './util.js';
import * as tractor from './tractor.js';
import * as vt from './vtpage.js';
import * as bus from './bus.js';
import * as variables from './variables.js';
import * as settings from './settings.js';

// Analyzer build (tools/build_demo.py --analyzer): only the Variabelen page, no simulator to connect to.
const analyzer = document.body.dataset.mode === 'analyzer';
const pages = analyzer ? { variabelen: variables } : { tractor, vt, bus, variabelen: variables, instellingen: settings };
const home = analyzer ? 'variabelen' : 'tractor';
let current = null;

if (analyzer) {
  for (const a of document.querySelectorAll('#tabs a')) if (!pages[a.getAttribute('href').slice(1)]) a.remove();
  for (const el of document.querySelectorAll('.page')) if (!pages[el.id.slice(5)]) el.remove();
  $('.brand').setAttribute('href', '#variabelen');
  $('#conn').hidden = true;
}
for (const [name, mod] of Object.entries(pages)) mod.mount($(`#page-${name}`));

function route() {
  const name = (location.hash || `#${home}`).slice(1);
  const target = pages[name] ? name : home;
  if (current === target) return;
  if (current) pages[current].hide?.();
  current = target;
  for (const el of document.querySelectorAll('.page')) el.hidden = el.id !== `page-${target}`;
  for (const a of document.querySelectorAll('#tabs a')) a.classList.toggle('on', a.getAttribute('href') === `#${target}`);
  pages[target].show?.();
}
window.addEventListener('hashchange', route);
route();

if (!analyzer) {
  onConnection((up) => {
    $('#conn').classList.toggle('on', up);
    $('#conn').title = up ? 'verbonden met de simulator' : 'geen verbinding met de simulator';
    if (up && current === 'bus') bus.reconnect();
    if (!up) toast('Verbinding met de simulator verbroken, opnieuw verbinden…', true);
  });
  on('toast', (m) => toast(m.text, m.error));
  on('bus', (m) => {
    const own = m.listen ? [] : m.cfs.filter((c) => c.enabled);
    const pills = own.map((c) => {
      const s = document.createElement('span');
      s.className = 'pill ' + (c.state === 'actief' ? 'ok' : c.state === 'claimen' ? 'warn' : 'err');
      s.textContent = `${{ tecu: 'TECU', vt: 'VT', gnss: 'GNSS' }[c.key]} ${c.address != null && c.address < 254 ? c.address.toString(16).toUpperCase() : '—'}`;
      s.title = `${c.label}: ${c.state}\n${c.nameText}`;
      return s;
    });
    if (m.listen) { // --listen: the simulator sends nothing at all
      const s = document.createElement('span');
      s.className = 'pill warn';
      s.textContent = 'Luistermodus';
      s.title = 'Gestart met --listen: de simulator zendt niets op de bus';
      pills.unshift(s);
    }
    $('#cfstat').replaceChildren(...pills);
  });
  connect();
}
window.tractorsim = { send };
