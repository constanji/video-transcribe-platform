class PcmCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const channels = inputs[0];
    if (!channels || !channels.length || !channels[0]) return true;
    const frames = channels[0].length;
    const mixed = new Float32Array(frames);
    for (let channel = 0; channel < channels.length; channel += 1) {
      const data = channels[channel];
      if (!data) continue;
      for (let i = 0; i < frames; i += 1) mixed[i] += data[i];
    }
    if (channels.length > 1) {
      const scale = 1 / channels.length;
      for (let i = 0; i < frames; i += 1) mixed[i] *= scale;
    }
    this.port.postMessage(mixed);
    return true;
  }
}

registerProcessor('pcm-capture', PcmCaptureProcessor);
