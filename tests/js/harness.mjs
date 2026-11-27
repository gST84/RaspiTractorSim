// Minimal test harness for the browser modules, runs headless with JavaScriptCore (macOS) or Node:
//   /System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc -m tests/js/run.mjs
//   node tests/js/run.mjs
export const out = typeof print === 'function' ? print : console.log;
let fails = 0, count = 0, group = '';

export function section(name) { group = name; out(`\n${name}`); }

export function test(name, fn) {
  count++;
  try {
    fn();
    out(`  ok   ${name}`);
  } catch (e) {
    fails++;
    out(`  FOUT ${name}: ${e && e.message ? e.message : e}`);
  }
}

/** For tests that wait on promises: `await testAsync(name, async () => …)`. */
export async function testAsync(name, fn) {
  count++;
  try {
    await fn();
    out(`  ok   ${name}`);
  } catch (e) {
    fails++;
    out(`  FOUT ${name}: ${e && e.message ? e.message : e}`);
  }
}

export function eq(got, want, what = '') {
  const g = JSON.stringify(got, (k, v) => (typeof v === 'bigint' ? `${v}n` : v instanceof Uint8Array ? Array.from(v) : v));
  const w = JSON.stringify(want, (k, v) => (typeof v === 'bigint' ? `${v}n` : v instanceof Uint8Array ? Array.from(v) : v));
  if (g !== w) throw new Error(`${what ? what + ': ' : ''}kreeg ${g}, verwacht ${w}`);
}

export function near(got, want, tol, what = '') {
  if (typeof got !== 'number' || Math.abs(got - want) > tol) throw new Error(`${what ? what + ': ' : ''}kreeg ${got}, verwacht ${want} ± ${tol}`);
}

export function ok(cond, what = 'voorwaarde') { if (!cond) throw new Error(what); }

export const summary = () => ({ fails, count, group });
