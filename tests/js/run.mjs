// JS tests of the analyzer (web/can):
//   /System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc -m tests/js/run.mjs
//   node tests/js/run.mjs
import { out, summary } from './harness.mjs';

// jsc has no performance / setTimeout in module scope everywhere: give the modules what they use
if (typeof globalThis.performance === 'undefined') globalThis.performance = { now: () => Date.now() };

await import('./test_core.mjs');
await import('./test_drivers.mjs');
await import('./test_dbc.mjs');
await import('./test_tc.mjs');
await import('./test_simbus.mjs');

const s = summary();
out(s.fails ? `\n${s.fails} van ${s.count} tests mislukt` : `\nAlle ${s.count} JS-tests geslaagd`);
if (s.fails) {
  if (typeof quit === 'function') quit(1);
  else if (typeof process !== 'undefined') process.exit(1);
}
