// The simulator / server as a frame source: every frame of its bus (SocketCAN, virtual bus or python-can)
// over the existing WebSocket, as the lossless subscription "raw".
//   server → page: { t: 'raw', rows: [[t, dir, id, hex, ext], …], lost, reset }

import { send, on, onConnection, isConnected } from '../../util.js';
import { frame } from '../frames.js';

let current = null;
let installed = false;

function install() {
  if (installed) return;
  installed = true;
  on('raw', (m) => current?.receive(m));
  onConnection((up) => {
    if (!current) return;
    if (up) send({ t: 'sub', name: 'raw', on: true });
    current.sink.status(up ? 'verbonden met de simulator' : 'verbinding met de simulator verbroken…');
  });
}

const bytes = (h) => {
  const out = new Uint8Array(h.length >> 1);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.substr(i * 2, 2), 16);
  return out;
};

export class ServerSource {
  constructor() { this.kind = 'server'; this.lost = 0; }
  get label() { return 'Simulator / server'; }
  static available() { return true; }

  async open(sink) {
    install();
    this.sink = sink;
    current = this;
    if (!isConnected()) sink.status('wachten op de simulator…');
    else sink.status('verbonden met de simulator');
    send({ t: 'sub', name: 'raw', on: true });
  }

  receive(m) {
    if (m.lost) { this.lost += m.lost; this.sink.lost?.(m.lost); }
    const frames = m.rows.map((r) => frame(r[0], r[2], bytes(r[3]), r[4] === undefined ? true : !!r[4], r[1]));
    if (frames.length) this.sink.frames(frames);
  }

  close() {
    if (current === this) current = null;
    send({ t: 'sub', name: 'raw', on: false });
  }
}
