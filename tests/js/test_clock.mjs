// The clock of the online simulator (web/can/clock.js): asyncio's waits, also run by events of the adapter.
import { section, test, eq } from './harness.mjs';
import { Clock } from '../../web/can/clock.js';

function fakeClock() {
  let t = 0;
  const timers = new Map();
  let id = 0;
  const clock = new Clock({
    worker: false,
    now: () => t,
    setTimer: (fn, ms) => { timers.set(++id, { fn, at: t + ms }); return id; },
    clearTimer: (i) => timers.delete(i),
  });
  return { clock, timers, set: (v) => { t = v; } };
}

section('Klok van de online simulator');
test('volgorde op tijdstip, gelijke tijd in volgorde van plannen', () => {
  const { clock, set } = fakeClock();
  const ran = [];
  clock.schedule(() => ran.push('b'), 200);
  clock.schedule(() => ran.push('a'), 100);
  clock.schedule(() => ran.push('c'), 200);
  set(250);
  clock.run();
  eq([ran, clock.pending], [['a', 'b', 'c'], 0]);
});
test('kick: een gebeurtenis van de adapter laat lopen wat klaar is, ook als de timer afgeremd is', () => {
  const { clock, timers, set } = fakeClock();
  const ran = [];
  clock.schedule(() => ran.push(1), 1000); // e.g. the VT Status every second
  set(500);
  clock.kick();
  eq(ran, [], 'nog niet klaar');
  set(1001); // the page timer is late (hidden window), but a CAN frame comes in
  clock.kick();
  eq([ran, clock.pending], [[1], 0]);
  clock.schedule(() => ran.push(2), 1000);
  eq([...timers.values()].map((x) => x.at), [2001], 'één timer, voor het vroegste');
});
test('de paginatimer blijft ook werken, en volgt het vroegste tijdstip', () => {
  const { clock, timers, set } = fakeClock();
  const ran = [];
  clock.schedule(() => ran.push('later'), 500);
  clock.schedule(() => ran.push('eerst'), 100);
  eq([...timers.values()].map((x) => x.at), [100]);
  set(100);
  const [first, { fn }] = [...timers.entries()][0];
  timers.delete(first); // a timer is gone once it fired
  fn();
  eq([ran, [...timers.values()].map((x) => x.at)], [['eerst'], [500]]);
});
