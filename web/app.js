import { $, connect, on, onConnection, toast, send } from './util.js';
import * as tractor from './tractor.js';
import * as vt from './vtpage.js';
import * as bus from './bus.js';
import * as settings from './settings.js';

const pages = { tractor, vt, bus, instellingen: settings };
let current = null;

for (const [name, mod] of Object.entries(pages)) mod.mount($(`#page-${name}`));

function route() {
  const name = (location.hash || '#tractor').slice(1);
  const target = pages[name] ? name : 'tractor';
  if (current === target) return;
  if (current) pages[current].hide?.();
  current = target;
  for (const el of document.querySelectorAll('.page')) el.hidden = el.id !== `page-${target}`;
  for (const a of document.querySelectorAll('#tabs a')) a.classList.toggle('on', a.getAttribute('href') === `#${target}`);
  pages[target].show?.();
}
window.addEventListener('hashchange', route);
route();

onConnection((up) => {
  $('#conn').classList.toggle('on', up);
  $('#conn').title = up ? 'verbonden met de simulator' : 'geen verbinding met de simulator';
  if (up && current === 'bus') bus.reconnect();
  if (!up) toast('Verbinding met de simulator verbroken, opnieuw verbinden…', true);
});
on('toast', (m) => toast(m.text, m.error));
on('bus', (m) => {
  const own = m.cfs.filter((c) => c.enabled);
  $('#cfstat').replaceChildren(...own.map((c) => {
    const s = document.createElement('span');
    s.className = 'pill ' + (c.state === 'actief' ? 'ok' : c.state === 'claimen' ? 'warn' : 'err');
    s.textContent = `${{ tecu: 'TECU', vt: 'VT', gnss: 'GNSS' }[c.key]} ${c.address != null && c.address < 254 ? c.address.toString(16).toUpperCase() : '—'}`;
    s.title = `${c.label}: ${c.state}\n${c.nameText}`;
    return s;
  }));
});
connect();
window.tractorsim = { send };
