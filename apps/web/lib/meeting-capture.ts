export type AudioSource = 'mic' | 'system' | 'both';

export type CaptureHandlers = {
  onLevel?: (value: number) => void;
  onSpeechSegment?: (pcm: Int16Array, offsetMs: number) => void;
  onStreamEnded?: (reason: string) => void;
};

const TARGET_RATE = 16000;
const MIN_SPEECH_MS = 800;
// 单块上限：MOSS 在 CPU 上的单块耗时 ≈ 5.8s 固定开销 + 0.51s/音频秒（Whisper 30s 窗 padding 造成）。
// 8s 块恒为 RTF 1.24（追不上说话速度，必然堆积）；16s 块 RTF≈0.87，留 12.8% 余量且仍在单个 30s 窗内。
const MAX_SPEECH_MS = 16000;
const SILENCE_HANG_MS = 450;
const SPEECH_RMS = 0.018;

function pickMimeType() {
  const types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'];
  return types.find((type) => typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(type)) || '';
}

function downsample(input: Float32Array, inputRate: number, outputRate: number) {
  if (inputRate === outputRate) return input;
  const ratio = inputRate / outputRate;
  const length = Math.max(1, Math.round(input.length / ratio));
  const output = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const position = i * ratio;
    const index = Math.floor(position);
    const next = Math.min(index + 1, input.length - 1);
    const weight = position - index;
    output[i] = input[index] * (1 - weight) + input[next] * weight;
  }
  return output;
}

function toPcm16(input: Float32Array) {
  const pcm = new Int16Array(input.length);
  for (let i = 0; i < input.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, input[i]));
    pcm[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return pcm;
}

function rms(input: Float32Array) {
  if (!input.length) return 0;
  let sum = 0;
  for (let i = 0; i < input.length; i += 1) sum += input[i] * input[i];
  return Math.sqrt(sum / input.length);
}

async function getMicrophone() {
  return navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    video: false,
  });
}

async function getSystemAudio() {
  const display = await navigator.mediaDevices.getDisplayMedia({
    video: true,
    audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
  });
  display.getVideoTracks().forEach((track) => track.stop());
  if (!display.getAudioTracks().length) {
    display.getTracks().forEach((track) => track.stop());
    throw new Error('未捕获到系统音频。请在弹窗中勾选「共享音频 / 同时分享系统声音」。标签页音频全平台可用；整屏系统声仅 Windows 可靠。');
  }
  return display;
}

export class MeetingCapture {
  private source: AudioSource;
  private live: boolean;
  private handlers: CaptureHandlers;
  private context: AudioContext | null = null;
  private micStream: MediaStream | null = null;
  private systemStream: MediaStream | null = null;
  private mixedStream: MediaStream | null = null;
  private recorder: MediaRecorder | null = null;
  private chunks: Blob[] = [];
  private node: AudioWorkletNode | null = null;
  private startedAt = 0;
  private pausedMs = 0;
  private pauseStarted = 0;
  private speaking = false;
  private speechStartSample = 0;
  private silenceMs = 0;
  private buffer: Int16Array[] = [];
  private bufferedSamples = 0;
  private totalSamples = 0;
  private paused = false;

  constructor(source: AudioSource, live: boolean, handlers: CaptureHandlers) {
    this.source = source;
    this.live = live;
    this.handlers = handlers;
  }

  elapsedMs() {
    if (!this.startedAt) return 0;
    const paused = this.paused ? Date.now() - this.pauseStarted : 0;
    return Math.max(0, Date.now() - this.startedAt - this.pausedMs - paused);
  }

  async start() {
    if (this.source === 'mic' || this.source === 'both') this.micStream = await getMicrophone();
    if (this.source === 'system' || this.source === 'both') this.systemStream = await getSystemAudio();
    this.context = new AudioContext();
    await this.context.resume();
    await this.context.audioWorklet.addModule('/pcm-worklet.js');
    const destination = this.context.createMediaStreamDestination();
    const mix = this.context.createGain();
    mix.connect(destination);
    if (this.micStream) this.context.createMediaStreamSource(this.micStream).connect(mix);
    if (this.systemStream) this.context.createMediaStreamSource(this.systemStream).connect(mix);
    this.node = new AudioWorkletNode(this.context, 'pcm-capture');
    const silent = this.context.createGain();
    silent.gain.value = 0;
    mix.connect(this.node);
    this.node.connect(silent);
    silent.connect(this.context.destination);
    this.node.port.onmessage = (event) => this.onAudio(event.data as Float32Array);
    this.mixedStream = destination.stream;
    const mimeType = pickMimeType();
    this.chunks = [];
    this.recorder = new MediaRecorder(this.mixedStream, mimeType ? { mimeType } : undefined);
    this.recorder.ondataavailable = (event) => {
      if (event.data.size) this.chunks.push(event.data);
    };
    this.watchTracks();
    this.startedAt = Date.now();
    this.recorder.start(1000);
  }

  pause() {
    if (this.paused) return;
    this.paused = true;
    this.pauseStarted = Date.now();
    if (this.recorder?.state === 'recording') this.recorder.pause();
    this.context?.suspend();
  }

  resume() {
    if (!this.paused) return;
    this.pausedMs += Date.now() - this.pauseStarted;
    this.paused = false;
    this.pauseStarted = 0;
    if (this.recorder?.state === 'paused') this.recorder.resume();
    this.context?.resume();
  }

  async stop() {
    this.flushSpeech(true);
    const blob = await new Promise<Blob>((resolve) => {
      if (!this.recorder || this.recorder.state === 'inactive') {
        resolve(new Blob(this.chunks, { type: this.recorder?.mimeType || 'audio/webm' }));
        return;
      }
      this.recorder.onstop = () => resolve(new Blob(this.chunks, { type: this.recorder?.mimeType || 'audio/webm' }));
      this.recorder.stop();
    });
    this.cleanup();
    const extension = blob.type.includes('mp4') ? 'm4a' : 'webm';
    return new File([blob], `meeting.${extension}`, { type: blob.type || 'audio/webm' });
  }

  private watchTracks() {
    const ended = () => this.handlers.onStreamEnded?.('屏幕共享已结束，录音已停止');
    this.systemStream?.getAudioTracks().forEach((track) => {
      track.addEventListener('ended', ended);
    });
  }

  private onAudio(frame: Float32Array) {
    if (this.paused || !this.context) return;
    const resampled = downsample(frame, this.context.sampleRate, TARGET_RATE);
    const level = rms(resampled);
    this.handlers.onLevel?.(Math.min(1, level * 8));
    if (!this.live) return;
    const pcm = toPcm16(resampled);
    const frameMs = (pcm.length / TARGET_RATE) * 1000;
    if (level >= SPEECH_RMS) {
      if (!this.speaking) {
        this.speaking = true;
        this.speechStartSample = this.totalSamples;
        this.buffer = [];
        this.bufferedSamples = 0;
      }
      this.silenceMs = 0;
      this.buffer.push(pcm);
      this.bufferedSamples += pcm.length;
    } else if (this.speaking) {
      this.buffer.push(pcm);
      this.bufferedSamples += pcm.length;
      this.silenceMs += frameMs;
      if (this.silenceMs >= SILENCE_HANG_MS) this.flushSpeech(false);
    }
    this.totalSamples += pcm.length;
    if (this.speaking && (this.bufferedSamples / TARGET_RATE) * 1000 >= MAX_SPEECH_MS) this.flushSpeech(false);
  }

  private flushSpeech(force: boolean) {
    if (!this.speaking) return;
    const durationMs = (this.bufferedSamples / TARGET_RATE) * 1000;
    if (!force && durationMs < MIN_SPEECH_MS) {
      this.resetSpeech();
      return;
    }
    const merged = new Int16Array(this.bufferedSamples);
    let offset = 0;
    for (const part of this.buffer) {
      merged.set(part, offset);
      offset += part.length;
    }
    const offsetMs = (this.speechStartSample / TARGET_RATE) * 1000;
    this.handlers.onSpeechSegment?.(merged, offsetMs);
    this.resetSpeech();
  }

  private resetSpeech() {
    this.speaking = false;
    this.silenceMs = 0;
    this.buffer = [];
    this.bufferedSamples = 0;
  }

  private cleanup() {
    this.node?.port.close();
    this.node?.disconnect();
    this.micStream?.getTracks().forEach((track) => track.stop());
    this.systemStream?.getTracks().forEach((track) => track.stop());
    this.mixedStream?.getTracks().forEach((track) => track.stop());
    this.context?.close();
    this.node = null;
    this.context = null;
    this.micStream = null;
    this.systemStream = null;
    this.mixedStream = null;
    this.recorder = null;
  }
}

export function pcm16ToBase64(pcm: Int16Array) {
  const bytes = new Uint8Array(pcm.buffer.slice(pcm.byteOffset, pcm.byteOffset + pcm.byteLength));
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}
