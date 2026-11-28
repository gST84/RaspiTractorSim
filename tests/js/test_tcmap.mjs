import { section, test, eq, near, ok } from './harness.mjs';
import { classes, classOf, niceStep, insideRings, fit, project, unproject, scaleBar, RAMP } from '../../web/tc/map.js';

section('Taakcontroller: kaart');

test('klassen van de legende: ronde grenzen die alles dekken', () => {
  const c = classes(112, 287);
  ok(c.length >= 3 && c.length <= 6, `aantal ${c.length}`);
  ok(c[0].lo <= 112 && c[c.length - 1].hi >= 287, JSON.stringify(c));
  eq(classOf(112, c), 0);
  eq(classOf(287, c), c.length - 1);
  eq(classes(200, 200).length, 1);
  eq(niceStep(175, 5), 50);
});

test('punt in polygoon met een gat', () => {
  const outer = [[0, 0], [10, 0], [10, 10], [0, 10]];
  const hole = [[4, 4], [6, 4], [6, 6], [4, 6]];
  ok(insideRings(2, 2, [outer, hole]));
  ok(!insideRings(5, 5, [outer, hole]));
  ok(!insideRings(12, 5, [outer]));
});

test('projectie heen en terug, noorden boven', () => {
  const v = fit([0, 0, 200, 100], 800, 400);
  const [x, y] = project(v, 800, 400, 200, 100);
  const [e, n] = unproject(v, 800, 400, x, y);
  near(e, 200, 1e-9);
  near(n, 100, 1e-9);
  ok(y < project(v, 800, 400, 200, 0)[1], 'noorden boven');
  ok(x <= 800 && x > 700, `rechts ${x}`);
});

test('schaalbalk en kleurtrappen', () => {
  const s = scaleBar(2);
  ok(s.px >= 50 && s.px <= 200, `${s.px}`);
  eq(RAMP.light.length, 5);
  eq(RAMP.dark.length, 5);
});
