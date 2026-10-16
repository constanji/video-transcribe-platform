'use client';

/**
 * 会议转录会话的全局单例。
 *
 * 录音采集（MediaStream/AudioWorklet）与实时转写 WebSocket 原本活在首页的
 * MeetingWorkbench 组件里，用户切到其他页面（即使客户端路由不刷新）组件一卸载就全断。
 * 这里把整套运行时提升为模块级单例：页面组件只是它的视图，跨页存活。
 *
 * 浏览器硬约束：整页刷新 / 关闭标签页仍会中断录音，无法规避。
 */

import { useSyncExternalStore } from 'react';
import { api } from './api';
import { MeetingCapture, pcm16ToBase64, type AudioSource } from './meeting-capture';
import { cancelJob, createMeeting, deleteMeeting, getMeeting, meetingWsUrl, retryTranscribe, uploadMeetingAudio } from './meeting-api';
import type { Job, MeetingRecord, MeetingSegment, VideoAsset } from './types';

export type MeetingMode = 'live' | 'recording';
export type MeetingPhase = 'idle' | 'starting' | 'recording' | 'paused' | 'uploading' | 'stopping' | 'processing' | 'ready';

// 单块确认超时 / 重试上限：没有它，任一帧丢失都会永久卡住整条有序队列。
const MAX_ATTEMPTS = 5;
const ACK_TIMEOUT_MS = 45000;
// 本地溢出队列上限（每块最多 16s → 最多约 128s 缓冲）。
const MAX_QUEUE = 8;
const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 15000];
const PING_INTERVAL_MS = 30000;
const MAX_FILE_BYTES = 500 * 1024 * 1024;

export { MAX_FILE_BYTES };

type OutChunk = {
  seq: number;
  pcm: Int16Array;
  offsetMs: number;
  endMs: number;
  attempts: number;
  holdCount: number;
  sent: boolean;
  timer: number | null;
};

export type MeetingSessionState = {
  mode: MeetingMode;
  phase: MeetingPhase;
  source: AudioSource;
  language: string;
  level: number;
  elapsed: number;
  segments: MeetingSegment[];
  meeting: MeetingRecord | null;
  video: VideoAsset | null;
  job: Job | null;
  busyHint: string;
  failure: string | null;
  lagSec: number;
  uploadProgress: number | null;
  cancelling: boolean;
  retrying: boolean;
  mossReady: boolean;
  mossMessage: string;
};

const INITIAL_STATE: MeetingSessionState = {
  mode: 'live',
  phase: 'idle',
  source: 'mic',
  language: 'zh',
  level: 0,
  elapsed: 0,
  segments: [],
  meeting: null,
  video: null,
  job: null,
  busyHint: '',
  failure: null,
  lagSec: 0,
  uploadProgress: null,
  cancelling: false,
  retrying: false,
  mossReady: false,
  mossMessage: '正在检查转录服务…',
};

type Timer = number | null;

class MeetingSessionManager {
  private state: MeetingSessionState = INITIAL_STATE;
  private listeners = new Set<() => void>();
  private snapshot: MeetingSessionState = INITIAL_STATE;

  private toastHandler: ((message: string, kind?: 'success' | 'error') => void) | null = null;

  private captureRef: MeetingCapture | null = null;
  private socketRef: WebSocket | null = null;
  private seqRef = 0;
  private inflightRef: OutChunk | null = null;
  private queueRef: OutChunk[] = [];
  private ackedEndRef = 0;
  private finishingRef = false;
  private reconnectRef = 0;
  private liveDegradedRef = false;
  private uploadAbortRef: AbortController | null = null;

  // 定时器：elapse/lag/心跳/终稿轮询/健康检查
  private elapsedTimer: Timer = null;
  private lagTimer: Timer = null;
  private pingTimer: Timer = null;
  private pollTimer: Timer = null;
  private healthTimer: Timer = null;
  private subscriberCount = 0;

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    this.subscriberCount += 1;
    if (this.subscriberCount === 1) this.ensureBackgroundLoops();
    return () => {
      this.listeners.delete(listener);
      this.subscriberCount -= 1;
      // 会话进行中即便暂时没人订阅（理论上悬浮条常驻不会发生）也不停后台循环。
      if (this.subscriberCount === 0 && this.state.phase === 'idle') this.stopBackgroundLoops();
    };
  };

  getSnapshot = () => this.snapshot;

  private notify(patch?: Partial<MeetingSessionState>) {
    if (patch) this.state = { ...this.state, ...patch };
    this.snapshot = { ...this.state };
    this.listeners.forEach((listener) => listener());
  }

  private toast(message: string, kind?: 'success' | 'error') {
    this.toastHandler?.(message, kind);
  }

  setToastHandler(handler: ((message: string, kind?: 'success' | 'error') => void) | null) {
    this.toastHandler = handler;
  }

  /** 首个订阅者出现时启动的健康检查 + 语言默认值加载（幂等）。 */
  private ensureBackgroundLoops() {
    if (this.healthTimer !== null) return;
    const tickHealth = async () => {
      try {
        const health = await api<any>('/api/system/health');
        const ready = Boolean(health.moss_reachable && health.moss_model_loaded);
        let message = '转录服务已就绪';
        if (!health.moss_reachable) message = '转录服务未连接';
        else if (health.moss_model_loading) message = '模型加载中，就绪后再开始';
        else if (!health.moss_model_loaded) message = '转录模型尚未加载';
        this.notify({ mossReady: ready, mossMessage: message });
      } catch {
        this.notify({ mossReady: false, mossMessage: '转录服务未连接' });
      }
    };
    tickHealth();
    this.healthTimer = window.setInterval(tickHealth, 4000);
    api<any>('/api/settings/transcription').then((settings) => {
      if (settings.default_language && this.state.phase === 'idle') this.setLanguage(settings.default_language);
    }).catch(() => undefined);
  }

  private stopBackgroundLoops() {
    if (this.healthTimer !== null) {
      window.clearInterval(this.healthTimer);
      this.healthTimer = null;
    }
  }

  // ---- 会话期间定时器（随 phase 同步启停）----

  private syncSessionTimers() {
    const phase = this.state.phase;
    const active = phase === 'recording' || phase === 'paused';

    if (active && this.elapsedTimer === null) {
      this.elapsedTimer = window.setInterval(() => {
        this.notify({ elapsed: this.captureRef?.elapsedMs() || 0 });
      }, 250);
    }
    if (!active && this.elapsedTimer !== null) {
      window.clearInterval(this.elapsedTimer);
      this.elapsedTimer = null;
    }

    if (active && this.lagTimer === null) {
      this.lagTimer = window.setInterval(() => {
        if (!this.ackedEndRef) {
          if (this.state.lagSec !== 0) this.notify({ lagSec: 0 });
          return;
        }
        const elapsedMs = this.captureRef?.elapsedMs() || 0;
        this.notify({ lagSec: Math.max(0, Math.round((elapsedMs - this.ackedEndRef) / 1000)) });
      }, 1000);
    }
    if (!active && this.lagTimer !== null) {
      window.clearInterval(this.lagTimer);
      this.lagTimer = null;
    }

    if (active && this.pingTimer === null) {
      this.pingTimer = window.setInterval(() => {
        const socket = this.socketRef;
        if (socket && socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: 'ping' }));
        }
      }, PING_INTERVAL_MS);
    }
    if (!active && this.pingTimer !== null) {
      window.clearInterval(this.pingTimer);
      this.pingTimer = null;
    }

    if (phase === 'processing' && this.pollTimer === null) {
      this.pollTimer = window.setInterval(() => { void this.pollProcessing(); }, 1500);
    }
    if (phase !== 'processing' && this.pollTimer !== null) {
      window.clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  private async pollProcessing() {
    const meeting = this.state.meeting;
    if (!meeting) return;
    try {
      const next = await getMeeting(meeting.id);
      const patch: Partial<MeetingSessionState> = { meeting: next };
      if (next.job) patch.job = next.job;
      if (next.video) patch.video = next.video;
      if (next.transcript?.segments?.length) patch.segments = next.transcript.segments;
      if (next.status === 'ready') {
        if (next.video_id) {
          const detail = await api<VideoAsset>(`/api/videos/${next.video_id}`);
          patch.video = detail;
        } else if (next.video) {
          patch.video = next.video;
        }
        patch.failure = null;
        patch.phase = 'ready';
        this.notify(patch);
        this.toast('终稿转录完成，可以生成会议纪要', 'success');
      } else if (
        next.status === 'failed' ||
        (next.job && (next.job.status === 'failed' || next.job.status === 'cancelled'))
      ) {
        // 任务侧失败但会议状态还没同步时也要给出出口，否则前端会永远停在「转录中」。
        const reason = next.error_message || next.job?.error_message || '终稿转录失败';
        patch.failure = reason;
        patch.phase = 'idle';
        this.notify(patch);
        this.toast(reason, 'error');
      } else {
        this.notify(patch);
      }
    } catch (err) {
      this.toast((err as Error).message, 'error');
    }
  }

  // ---- 简单 setter ----

  setMode(mode: MeetingMode) {
    if (this.state.phase === 'idle') this.notify({ mode });
  }

  setSource(source: AudioSource) {
    if (this.state.phase === 'idle') this.notify({ source });
  }

  setLanguage(language: string) {
    this.notify({ language });
  }

  togglePause() {
    if (this.state.phase === 'paused') {
      this.captureRef?.resume();
      this.notify({ phase: 'recording' });
    } else if (this.state.phase === 'recording') {
      this.captureRef?.pause();
      this.notify({ phase: 'paused', level: 0 });
    }
    this.syncSessionTimers();
  }

  // ---- 分片队列（自 MeetingWorkbench 原样搬运）----

  private clearChunkTimers() {
    if (this.inflightRef?.timer) window.clearTimeout(this.inflightRef.timer);
    this.queueRef.forEach((chunk) => {
      if (chunk.timer) window.clearTimeout(chunk.timer);
    });
  }

  private onChunkTimeout(chunk: OutChunk) {
    if (this.inflightRef !== chunk) return;
    if (chunk.attempts >= MAX_ATTEMPTS) {
      this.dropChunk(chunk, '转写超时');
      return;
    }
    chunk.sent = false;
    this.sendChunk(chunk, 1500);
  }

  private sendChunk(chunk: OutChunk, delayMs: number) {
    if (chunk.timer) {
      window.clearTimeout(chunk.timer);
      chunk.timer = null;
    }
    const fire = () => {
      chunk.timer = null;
      if (this.inflightRef !== chunk) return;
      const socket = this.socketRef;
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        // 断线：留在在途，重连成功后由 pump 补发；长时间连不上则丢弃。
        chunk.sent = false;
        chunk.holdCount += 1;
        if (chunk.holdCount > 30) {
          this.dropChunk(chunk, '通道断开');
          return;
        }
        chunk.timer = window.setTimeout(fire, 1500);
        return;
      }
      chunk.attempts += 1;
      chunk.sent = true;
      chunk.holdCount = 0;
      socket.send(JSON.stringify({
        type: 'audio',
        seq: chunk.seq,
        sample_rate: 16000,
        offset_ms: Math.round(chunk.offsetMs),
        pcm16: pcm16ToBase64(chunk.pcm),
      }));
      chunk.timer = window.setTimeout(() => this.onChunkTimeout(chunk), ACK_TIMEOUT_MS);
    };
    if (delayMs > 0) chunk.timer = window.setTimeout(fire, delayMs);
    else fire();
  }

  private dropChunk(chunk: OutChunk, why: string) {
    if (chunk.timer) window.clearTimeout(chunk.timer);
    if (this.inflightRef === chunk) this.inflightRef = null;
    this.toast(`有一段音频${why}，已跳过`, 'error');
    this.pump();
  }

  // 服务端串行处理，客户端只允许一块在途：避免 WS 缓冲无界堆积。
  private pump() {
    const socket = this.socketRef;
    const open = Boolean(socket && socket.readyState === WebSocket.OPEN);
    const inflight = this.inflightRef;
    if (inflight) {
      if (open && !inflight.sent) this.sendChunk(inflight, 0);
      return;
    }
    if (!open) return;
    const next = this.queueRef.shift();
    if (!next) return;
    this.seqRef += 1;
    next.seq = this.seqRef;
    this.inflightRef = next;
    this.sendChunk(next, 0);
  }

  private enqueue = (pcm: Int16Array, offsetMs: number) => {
    if (this.queueRef.length >= MAX_QUEUE) {
      // 不再把块无限合并成超长请求。完整录音仍由 MediaRecorder 保存，停止后终稿不会丢。
      if (!this.liveDegradedRef) {
        this.liveDegradedRef = true;
        this.notify({ busyHint: '实时字幕已跟不上，将以停录后的终稿为准' });
        this.toast('实时字幕延迟过高，已暂停发送新字幕块；完整录音仍会保存', 'error');
      }
      return;
    }
    const endMs = offsetMs + (pcm.length / 16000) * 1000;
    this.queueRef.push({ seq: 0, pcm, offsetMs, endMs, attempts: 0, holdCount: 0, sent: false, timer: null });
    this.pump();
  };

  private handleSocketMessage(payload: any, succeed: () => void) {
    if (payload.type === 'ready') {
      this.notify({ busyHint: '' });
      succeed();
      return;
    }
    if (payload.type === 'pong') return;
    if (payload.type === 'busy') {
      this.notify({ busyHint: '转录服务繁忙，正在排队…' });
      const inflight = this.inflightRef;
      if (inflight && inflight.seq === payload.seq) {
        if (inflight.attempts >= MAX_ATTEMPTS) this.dropChunk(inflight, '因服务繁忙超时');
        else this.sendChunk(inflight, Math.max(1000, (Number(payload.retry_after) || 2) * 1000));
      }
      return;
    }
    if (payload.type === 'segment') {
      this.notify({
        busyHint: '',
        ...(payload.segments?.length ? { segments: [...this.state.segments, ...payload.segments] } : {}),
      });
      const inflight = this.inflightRef;
      if (inflight && inflight.seq === payload.seq) {
        if (inflight.timer) window.clearTimeout(inflight.timer);
        this.ackedEndRef = inflight.endMs;
        this.inflightRef = null;
        this.pump();
      }
      return;
    }
    if (payload.type === 'error') {
      const inflight = this.inflightRef;
      if (payload.seq && inflight && inflight.seq === payload.seq) {
        if (inflight.attempts >= MAX_ATTEMPTS) this.dropChunk(inflight, '转写失败');
        else this.sendChunk(inflight, 2000);
      } else {
        this.toast(payload.message || '实时转写出错', 'error');
      }
    }
  }

  private scheduleReconnect() {
    const phase = this.state.phase;
    if (this.finishingRef || (phase !== 'recording' && phase !== 'paused')) return;
    const attempt = this.reconnectRef;
    if (attempt >= RECONNECT_DELAYS.length) {
      this.toast('实时转写通道断开，已放弃重连', 'error');
      return;
    }
    this.reconnectRef = attempt + 1;
    this.toast('实时转写通道断开，正在重连…');
    window.setTimeout(async () => {
      if (this.finishingRef) return;
      const meetingId = this.state.meeting?.id;
      if (!meetingId) return;
      try {
        await this.openSocket(meetingId);
        this.reconnectRef = 0;
        // 断线期间在途块可能已发出但收不到确认：立即用同一 seq 重发（服务端幂等去重）。
        if (this.inflightRef) this.inflightRef.sent = false;
        this.pump();
        this.toast('实时转写通道已恢复', 'success');
      } catch {
        this.scheduleReconnect();
      }
    }, RECONNECT_DELAYS[attempt]);
  }

  private openSocket(meetingId: string) {
    return new Promise<void>((resolve, reject) => {
      const socket = new WebSocket(meetingWsUrl(meetingId));
      this.socketRef = socket;
      let settled = false;
      let opened = false;
      const timer = window.setTimeout(() => {
        if (settled) return;
        settled = true;
        reject(new Error('实时转写通道超时'));
      }, 8000);
      const succeed = () => {
        if (settled) return;
        settled = true;
        opened = true;
        window.clearTimeout(timer);
        resolve();
      };
      socket.onerror = () => {
        window.clearTimeout(timer);
        if (settled) return;
        settled = true;
        reject(new Error('实时转写通道连接失败'));
      };
      socket.onmessage = (event) => {
        try {
          this.handleSocketMessage(JSON.parse(event.data), succeed);
        } catch {
          this.toast('实时转写消息无法解析', 'error');
        }
      };
      socket.onclose = () => {
        if (this.socketRef === socket) this.socketRef = null;
        window.clearTimeout(timer);
        if (!settled) {
          settled = true;
          reject(new Error('实时转写通道已断开'));
          return;
        }
        // 仅在「连接曾建立后又断开」时重连；建连失败由调用方 catch 触发。
        if (opened) this.scheduleReconnect();
      };
    });
  }

  private async drain(timeoutMs: number) {
    const deadline = Date.now() + timeoutMs;
    while ((this.inflightRef || this.queueRef.length) && Date.now() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, 200));
    }
    return !this.inflightRef && this.queueRef.length === 0;
  }

  // ---- 对外动作 ----

  async start() {
    const { mossReady, mossMessage, mode, language, source } = this.state;
    if (!mossReady) {
      this.toast(mossMessage, 'error');
      return;
    }
    this.notify({ failure: null, phase: 'starting' });
    try {
      const created = await createMeeting({ mode, language, audio_source: source });
      if (mode === 'live') await this.openSocket(created.id);
      const capture = new MeetingCapture(source, mode === 'live', {
        onLevel: (level) => this.notify({ level }),
        onSpeechSegment: this.enqueue,
        onStreamEnded: async (reason) => {
          this.toast(reason, 'error');
          if (this.captureRef) await this.finish();
        },
      });
      this.captureRef = capture;
      await capture.start();
      this.notify({ meeting: created, phase: 'recording' });
      this.toast(mode === 'live' ? '已开始实时转录' : '已开始录音', 'success');
    } catch (err) {
      this.captureRef = null;
      this.socketRef?.close();
      this.socketRef = null;
      this.notify({ phase: 'idle' });
      this.toast((err as Error).message, 'error');
    }
  }

  async finish() {
    const capture = this.captureRef;
    const current = this.state.meeting;
    if (!capture || !current) return;
    this.notify({ phase: 'stopping' });
    this.syncSessionTimers();
    this.finishingRef = true;
    try {
      // ① 先停止采集：capture.stop() 内部 flushSpeech(true) 会把最后不足一块的语音也入队。
      const file = await capture.stop();
      this.captureRef = null;
      // ② 等在途与队列全部转写完成，再发 WS stop。
      //    服务端 WS 循环是全串行的，「stop」排在最后一块 audio 之后才能保证它被处理入库。
      const drained = await this.drain(30000);
      if (!drained) {
        const left = this.queueRef.length + (this.inflightRef ? 1 : 0);
        this.toast(`仍有 ${left} 段音频未转写完，已跳过`, 'error');
      }
      if (this.socketRef?.readyState === WebSocket.OPEN) {
        this.socketRef.send(JSON.stringify({ type: 'stop' }));
      }
      this.socketRef?.close();
      this.socketRef = null;
      // ③ 先无条件落盘音频（transcribe=false，与转写解耦），保证「停止即保存」。
      //    即使 MOSS 忙/未就绪，录音文件也已持久化，可稍后用重试按钮重跑转写。
      this.notify({ uploadProgress: 0 });
      const saved = await uploadMeetingAudio(current.id, file, false, (percent) => this.notify({ uploadProgress: percent }));
      this.notify({ uploadProgress: null, meeting: saved, video: saved.video || this.state.video });
      // ④ 落盘成功后再单独启动终稿转录。
      if (this.state.mossReady) {
        this.notify({ phase: 'processing' });
        this.syncSessionTimers();
        const next = await retryTranscribe(current.id);
        this.notify({ meeting: next, job: next.job || null, video: next.video || this.state.video });
        this.toast('录音已保存，正在生成终稿转录', 'success');
      } else {
        this.notify({
          failure: '录音已保存，但转录服务未就绪，可稍后点击「重试终稿转录」',
          phase: 'idle',
        });
        this.syncSessionTimers();
        this.toast(this.state.mossMessage, 'error');
      }
    } catch (err) {
      // 音频可能已部分落盘；失败时不误报「没保存」，仅提示转写未完成。
      this.notify({ failure: '录音可能未完整保存，请重试录制', phase: 'idle' });
      this.syncSessionTimers();
      this.toast((err as Error).message, 'error');
    } finally {
      this.notify({ uploadProgress: null });
      this.finishingRef = false;
    }
  }

  async cancelTranscription() {
    const currentJob = this.state.job;
    if (!currentJob) return;
    this.notify({ cancelling: true });
    try {
      const updated = await cancelJob(currentJob.id);
      this.notify({
        job: updated,
        failure: '任务已取消，可以在下面重新开始终稿转录',
        phase: 'idle',
      });
      this.syncSessionTimers();
      this.toast('已取消终稿转录', 'success');
    } catch (err) {
      this.toast((err as Error).message, 'error');
    } finally {
      this.notify({ cancelling: false });
    }
  }

  // 终稿转录失败/中断后的重试：录音文件还在，直接重跑，不必重新录制。
  async retryFinalTranscript() {
    const current = this.state.meeting;
    if (!current) return;
    if (!this.state.mossReady) {
      this.toast(this.state.mossMessage, 'error');
      return;
    }
    this.notify({ retrying: true });
    try {
      const next = await retryTranscribe(current.id);
      this.notify({
        meeting: next,
        job: next.job || null,
        video: next.video || this.state.video,
        failure: null,
        phase: 'processing',
      });
      this.syncSessionTimers();
      this.toast('已重新启动终稿转录', 'success');
    } catch (err) {
      this.toast((err as Error).message, 'error');
    } finally {
      this.notify({ retrying: false });
    }
  }

  async importFile(file: File) {
    if (file.size > MAX_FILE_BYTES) {
      this.toast('单个文件不能超过 500MB', 'error');
      return;
    }
    if (!this.state.mossReady) {
      this.toast(this.state.mossMessage, 'error');
      return;
    }
    this.notify({ failure: null, phase: 'starting' });
    let createdId: string | null = null;
    try {
      const created = await createMeeting({
        mode: 'recording',
        language: this.state.language,
        audio_source: this.state.source,
        title: file.name.replace(/\.[^.]+$/, ''),
      });
      createdId = created.id;
      this.notify({ meeting: created, phase: 'uploading', uploadProgress: 0 });
      const controller = new AbortController();
      this.uploadAbortRef = controller;
      const next = await uploadMeetingAudio(
        created.id,
        file,
        true,
        (percent) => this.notify({ uploadProgress: percent }),
        controller.signal,
      );
      this.uploadAbortRef = null;
      this.notify({
        uploadProgress: null,
        phase: 'processing',
        meeting: next,
        job: next.job || null,
        video: next.video || this.state.video,
      });
      this.syncSessionTimers();
      this.toast('文件已导入，正在转录', 'success');
    } catch (err) {
      this.uploadAbortRef = null;
      if ((err as Error).message === '上传已取消' && createdId) {
        await deleteMeeting(createdId).catch(() => undefined);
      }
      this.notify({ uploadProgress: null, phase: 'idle' });
      this.syncSessionTimers();
      this.toast((err as Error).message, 'error');
    }
  }

  abortUpload() {
    this.uploadAbortRef?.abort();
  }

  resetSession() {
    this.clearChunkTimers();
    this.socketRef?.close();
    this.socketRef = null;
    this.captureRef = null;
    this.seqRef = 0;
    this.inflightRef = null;
    this.queueRef = [];
    this.ackedEndRef = 0;
    this.finishingRef = false;
    this.reconnectRef = 0;
    this.liveDegradedRef = false;
    this.uploadAbortRef?.abort();
    this.uploadAbortRef = null;
    this.notify({
      busyHint: '',
      failure: null,
      lagSec: 0,
      elapsed: 0,
      level: 0,
      segments: [],
      meeting: null,
      video: null,
      job: null,
      uploadProgress: null,
      phase: 'idle',
    });
    this.syncSessionTimers();
  }
}

export const meetingSession = new MeetingSessionManager();

export function useMeetingSession(): MeetingSessionState {
  return useSyncExternalStore(meetingSession.subscribe, meetingSession.getSnapshot, () => INITIAL_STATE);
}
