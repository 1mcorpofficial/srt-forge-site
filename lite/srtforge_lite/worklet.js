class Capture extends AudioWorkletProcessor {
  constructor() { super(); this.buf = []; this.n = 0; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) { this.buf.push(ch.slice()); this.n += ch.length;
      if (this.n >= sampleRate / 4) { const out = new Float32Array(this.n); let o = 0; for (const b of this.buf) { out.set(b, o); o += b.length; } this.port.postMessage(out, [out.buffer]); this.buf = []; this.n = 0; } }
    return true;
  }
}
registerProcessor("capture", Capture);
