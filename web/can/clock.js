// The clock of the simulator in the online version: the waits of asyncio (Pyodide's scheduleCallback, see
// tractorsim/browser.py use_clock) are kept here, ordered by due time.
// Chrome slows down the timers of a hidden or covered page (to once a second, after five minutes to once a minute),
// so the periodic messages and the VT Status of the simulator would stop and a VT client drops the VT. Therefore the
// due callbacks run on whatever comes first:
//   - a timer of the page (enough while the window is visible);
//   - a timer in a web worker (in some browsers not slowed down);
//   - kick(): every USB or serial event of the CAN adapter (received frames, keep-alives, finished sends). Events
//     are not slowed down, and on a bus with a PLC they come many times per second.

export class Clock {
  /** opts.now / opts.setTimer / opts.clearTimer for tests; opts.worker = false: no worker. */
  constructor(opts = {}) {
    this.now = opts.now || (() => performance.now());
    this.setTimer = opts.setTimer || ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = opts.clearTimer || ((id) => clearTimeout(id));
    this.queue = []; // { due, seq, cb }, earliest first
    this.seq = 0;
    this.timer = null;
    this.armedAt = Infinity;
    this.worker = null;
    if (opts.worker !== false && typeof Worker !== 'undefined' && typeof Blob !== 'undefined') {
      try {
        this.worker = new Worker(URL.createObjectURL(new Blob(
          ['let t = null; onmessage = (e) => { clearTimeout(t); t = setTimeout(() => postMessage(0), e.data); };'],
          { type: 'text/javascript' })));
        this.worker.onmessage = () => this.run();
      } catch { this.worker = null; } // no workers here: page timer and kicks only
    }
  }

  /** Calls `cb` once, `ms` milliseconds from now. */
  schedule(cb, ms) {
    const e = { due: this.now() + Math.max(0, ms), seq: ++this.seq, cb };
    let i = this.queue.length;
    while (i > 0 && this.queue[i - 1].due > e.due) i--;
    this.queue.splice(i, 0, e);
    if (i === 0) this.arm();
  }

  /** Runs every callback that is due (a timer fired or the adapter did something). */
  run() {
    const now = this.now();
    let n = 0;
    while (this.queue.length && this.queue[0].due <= now && n++ < 1000) {
      const e = this.queue.shift();
      try { e.cb(); } catch (err) { console.error('klok', err); }
    }
    if (this.timer !== null) { this.clearTimer(this.timer); this.timer = null; this.armedAt = Infinity; }
    this.arm();
  }

  /** An event of the adapter: run what is due, without waiting for a (slowed down) timer. */
  kick() {
    if (this.queue.length && this.queue[0].due <= this.now()) this.run();
  }

  get pending() { return this.queue.length; }

  arm() {
    const next = this.queue[0];
    if (!next) return;
    if (this.timer !== null && this.armedAt <= next.due) return;
    if (this.timer !== null) this.clearTimer(this.timer);
    const ms = Math.max(0, next.due - this.now());
    this.armedAt = next.due;
    this.timer = this.setTimer(() => { this.timer = null; this.armedAt = Infinity; this.run(); }, ms);
    this.worker?.postMessage(ms);
  }
}
