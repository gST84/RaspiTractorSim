// JS tests of the analyzer (web/can):
//   /System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc -m tests/js/run.mjs
//   node tests/js/run.mjs
import { out, summary } from './harness.mjs';

// jsc has no performance / setTimeout in module scope everywhere: give the modules what they use
if (typeof globalThis.performance === 'undefined') globalThis.performance = { now: () => Date.now() };
// jsc has setTimeout but no clearTimeout: a cancelled timer still runs out, but its callback is skipped
if (typeof globalThis.clearTimeout === 'undefined') {
  const real = globalThis.setTimeout;
  const cancelled = new Set();
  let n = 0;
  globalThis.setTimeout = (fn, ms, ...args) => { const id = ++n; real(() => { if (!cancelled.delete(id)) fn(...args); }, ms); return id; };
  globalThis.clearTimeout = (id) => { cancelled.add(id); };
}

await import('./test_core.mjs');
await import('./test_drivers.mjs');
await import('./test_dbc.mjs');
await import('./test_tc.mjs');
await import('./test_simbus.mjs');
await import('./test_clock.mjs');

const s = summary();
out(s.fails ? `\n${s.fails} van ${s.count} tests mislukt` : `\nAlle ${s.count} JS-tests geslaagd`);
if (s.fails) {
  if (typeof quit === 'function') quit(1);
  else if (typeof process !== 'undefined') process.exit(1);
}
