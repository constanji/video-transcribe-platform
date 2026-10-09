'use client';

import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api';
import { MeetingCapture, pcm16ToBase64, type AudioSource } from '../lib/meeting-capture';
import { cancelJob, createMeeting, getMeeting, meetingWsUrl, retryTranscribe, uploadMeetingAudio } from '../lib/meeting-api';
import type { Job, MeetingRecord, MeetingSegment, Platform, VideoAsset } from '../lib/types';
import ParsedVideoCard from './ParsedVideoCard';
import SummaryProgress from './SummaryProgress';

type Mode = 'live' | 'recording';
type Phase = 'idle' | 'starting' | 'recording' | 'paused' | 'stopping' | 'processing' | 'ready';

const ACCEPT = '.mp3,.wav,.m4a,.mp4,.webm,.mov';

// 单块确认超时 / 重试上限：没有它，任一帧丢失都会永久卡住整条有序队列。
const MAX_ATTEMPTS = 5;
const ACK_TIMEOUT_MS = 45000;
// 本地溢出队列上限（每块最多 16s → 最多约 128s 缓冲）。
const MAX_QUEUE = 8;
const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 15000];
const PING_INTERVAL_MS = 30000;

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

function formatClock(ms: number) {
  const total = Math.floor(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

function formatStamp(ms?: number) {
  if (!ms && ms !== 0) return '';
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

function sourceHint(source: AudioSource) {
  if (source === 'mic') return '使用麦克风采集现场人声。无法录到电脑内部播放的声音。';
  if (source === 'system') return '将弹出屏幕共享。必须勾选「共享音频」。标签页音频全平台可用；整屏系统声仅 Windows 可靠。';
  return '同时采集麦克风和系统音频。系统声仍需在弹窗中勾选「共享音频」。';
}

export default function MeetingWorkbench({
  platforms,
  onToast,
}: {
  platforms: Platform[];
  onToast: (message: string, kind?: 'success' | 'error') => void;
}) {
  const [mode, setMode] = useState<Mode>('live');
  const [phase, setPhase] = useState<Phase>('idle');
  const [source, setSource] = useState<AudioSource>('mic');
  const [language, setLanguage] = useState('zh');
  const [level, setLevel] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [segments, setSegments] = useState<MeetingSegment[]>([]);
  const [meeting, setMeeting] = useState<MeetingRecord | null>(null);
  const [video, setVideo] = useState<VideoAsset | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [mossReady, setMossReady] = useState(false);
  const [mossMessage, setMossMessage] = useState('正在检查转录服务…');
  const [lagSec, setLagSec] = useState(0);
  const [busyHint, setBusyHint] = useState('');
  const [failure, setFailure] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const captureRef = useRef<MeetingCapture | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const seqRef = useRef(0);
  const inflightRef = useRef<OutChunk | null>(null);
  const queueRef = useRef<OutChunk[]>([]);
  const ackedEndRef = useRef(0);
  const finishingRef = useRef(false);
  const reconnectRef = useRef(0);
  const phaseRef = useRef<Phase>('idle');
  const meetingIdRef = useRef<string | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const busy = phase === 'starting' || phase === 'stopping';

  useEffect(() => {
    phaseRef.current = phase;
  }, [phase]);

  useEffect(() => {
    meetingIdRef.current = meeting?.id || null;
  }, [meeting?.id]);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const health = await api<any>('/api/system/health');
        if (cancelled) return;
        setMossReady(Boolean(health.moss_reachable && health.moss_model_loaded));
        if (!health.moss_reachable) setMossMessage('转录服务未连接');
        else if (health.moss_model_loading) setMossMessage('模型加载中，就绪后再开始');
        else if (!health.moss_model_loaded) setMossMessage('转录模型尚未加载');
        else setMossMessage('转录服务已就绪');
      } catch {
        if (!cancelled) {
          setMossReady(false);
          setMossMessage('转录服务未连接');
        }
      }
    }
    tick();
    const timer = window.setInterval(tick, 4000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    api<any>('/api/settings/transcription').then((settings) => {
      if (settings.default_language) setLanguage(settings.default_language);
    }).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (phase !== 'recording' && phase !== 'paused') return;
    const timer = window.setInterval(() => {
      setElapsed(captureRef.current?.elapsedMs() || 0);
    }, 250);
    return () => window.clearInterval(timer);
  }, [phase]);

  // 暂停时采集已停止，不再产生电平帧；主动归零，避免电平条停在最后一次的值。
  useEffect(() => {
    if (phase === 'paused') setLevel(0);
  }, [phase]);

  // 滞后提示：已录制时长减去最后一块已确认字幕的结束时间。
  useEffect(() => {
    if (phase !== 'recording' && phase !== 'paused') {
      setLagSec(0);
      return;
    }
    const timer = window.setInterval(() => {
      if (!ackedEndRef.current) {
        setLagSec(0);
        return;
      }
      const elapsedMs = captureRef.current?.elapsedMs() || 0;
      setLagSec(Math.max(0, Math.round((elapsedMs - ackedEndRef.current) / 1000)));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [phase]);

  // 心跳：长时间静默不发言时也刷新服务端 updated_at，避免会议被僵尸清扫误判。
  useEffect(() => {
    if (phase !== 'recording' && phase !== 'paused') return;
    const timer = window.setInterval(() => {
      const socket = socketRef.current;
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'ping' }));
      }
    }, PING_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [phase]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' });
  }, [segments.length]);

  useEffect(() => {
    if (phase !== 'processing' || !meeting?.id) return;
    const timer = window.setInterval(async () => {
      try {
        const next = await getMeeting(meeting.id);
        setMeeting(next);
        if (next.job) setJob(next.job);
        if (next.video) setVideo(next.video);
        if (next.transcript?.segments?.length) setSegments(next.transcript.segments);
        if (next.status === 'ready') {
          if (next.video_id) {
            const detail = await api<VideoAsset>(`/api/videos/${next.video_id}`);
            setVideo(detail);
          } else if (next.video) {
            setVideo(next.video);
          }
          setFailure(null);
          setPhase('ready');
          onToast('终稿转录完成，可以生成会议纪要');
        } else if (
          next.status === 'failed' ||
          (next.job && (next.job.status === 'failed' || next.job.status === 'cancelled'))
        ) {
          // 任务侧失败但会议状态还没同步时也要给出出口，否则前端会永远停在「转录中」。
          const reason = next.error_message || next.job?.error_message || '终稿转录失败';
          setFailure(reason);
          setPhase('idle');
          onToast(reason, 'error');
        }
      } catch (err) {
        onToast((err as Error).message, 'error');
      }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [meeting?.id, phase, onToast]);

  function clearTimers() {
    if (inflightRef.current?.timer) window.clearTimeout(inflightRef.current.timer);
    queueRef.current.forEach((chunk) => {
      if (chunk.timer) window.clearTimeout(chunk.timer);
    });
  }

  function resetSession() {
    clearTimers();
    socketRef.current?.close();
    socketRef.current = null;
    captureRef.current = null;
    seqRef.current = 0;
    inflightRef.current = null;
    queueRef.current = [];
    ackedEndRef.current = 0;
    finishingRef.current = false;
    reconnectRef.current = 0;
    setBusyHint('');
    setFailure(null);
    setLagSec(0);
    setElapsed(0);
    setLevel(0);
    setSegments([]);
    setMeeting(null);
    setVideo(null);
    setJob(null);
    setPhase('idle');
  }

  function onChunkTimeout(chunk: OutChunk) {
    if (inflightRef.current !== chunk) return;
    if (chunk.attempts >= MAX_ATTEMPTS) {
      dropChunk(chunk, '转写超时');
      return;
    }
    chunk.sent = false;
    sendChunk(chunk, 1500);
  }

  function sendChunk(chunk: OutChunk, delayMs: number) {
    if (chunk.timer) {
      window.clearTimeout(chunk.timer);
      chunk.timer = null;
    }
    const fire = () => {
      chunk.timer = null;
      if (inflightRef.current !== chunk) return;
      const socket = socketRef.current;
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        // 断线：留在在途，重连成功后由 pump 补发；长时间连不上则丢弃。
        chunk.sent = false;
        chunk.holdCount += 1;
        if (chunk.holdCount > 30) {
          dropChunk(chunk, '通道断开');
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
      chunk.timer = window.setTimeout(() => onChunkTimeout(chunk), ACK_TIMEOUT_MS);
    };
    if (delayMs > 0) chunk.timer = window.setTimeout(fire, delayMs);
    else fire();
  }

  function dropChunk(chunk: OutChunk, why: string) {
    if (chunk.timer) window.clearTimeout(chunk.timer);
    if (inflightRef.current === chunk) inflightRef.current = null;
    onToast(`有一段音频${why}，已跳过`, 'error');
    pump();
  }

  // 服务端串行处理，客户端只允许一块在途：避免 WS 缓冲无界堆积。
  function pump() {
    const socket = socketRef.current;
    const open = Boolean(socket && socket.readyState === WebSocket.OPEN);
    const inflight = inflightRef.current;
    if (inflight) {
      if (open && !inflight.sent) sendChunk(inflight, 0);
      return;
    }
    if (!open) return;
    const next = queueRef.current.shift();
    if (!next) return;
    seqRef.current += 1;
    next.seq = seqRef.current;
    inflightRef.current = next;
    sendChunk(next, 0);
  }

  // 队列超限时合并最旧两块；用静音补齐间隔，保持绝对时间轴不漂移。
  function mergeFront() {
    const queue = queueRef.current;
    if (queue.length < 2) return;
    const first = queue[0];
    const second = queue[1];
    const gapSamples = Math.max(0, Math.round(((second.offsetMs - first.endMs) / 1000) * 16000));
    const merged = new Int16Array(first.pcm.length + gapSamples + second.pcm.length);
    merged.set(first.pcm, 0);
    merged.set(second.pcm, first.pcm.length + gapSamples);
    queue.splice(0, 2, { ...first, pcm: merged, endMs: second.endMs, timer: null });
  }

  function enqueue(pcm: Int16Array, offsetMs: number) {
    const endMs = offsetMs + (pcm.length / 16000) * 1000;
    queueRef.current.push({ seq: 0, pcm, offsetMs, endMs, attempts: 0, holdCount: 0, sent: false, timer: null });
    while (queueRef.current.length > MAX_QUEUE) mergeFront();
    pump();
  }

  function handleSocketMessage(payload: any, succeed: () => void) {
    if (payload.type === 'ready') {
      setBusyHint('');
      succeed();
      return;
    }
    if (payload.type === 'pong') return;
    if (payload.type === 'busy') {
      setBusyHint('转录服务繁忙，正在排队…');
      const inflight = inflightRef.current;
      if (inflight && inflight.seq === payload.seq) {
        if (inflight.attempts >= MAX_ATTEMPTS) dropChunk(inflight, '因服务繁忙超时');
        else sendChunk(inflight, Math.max(1000, (Number(payload.retry_after) || 2) * 1000));
      }
      return;
    }
    if (payload.type === 'segment') {
      setBusyHint('');
      if (payload.segments?.length) {
        setSegments((current) => [...current, ...payload.segments]);
      }
      const inflight = inflightRef.current;
      if (inflight && inflight.seq === payload.seq) {
        if (inflight.timer) window.clearTimeout(inflight.timer);
        ackedEndRef.current = inflight.endMs;
        inflightRef.current = null;
        pump();
      }
      return;
    }
    if (payload.type === 'error') {
      const inflight = inflightRef.current;
      if (payload.seq && inflight && inflight.seq === payload.seq) {
        if (inflight.attempts >= MAX_ATTEMPTS) dropChunk(inflight, '转写失败');
        else sendChunk(inflight, 2000);
      } else {
        onToast(payload.message || '实时转写出错', 'error');
      }
    }
  }

  function scheduleReconnect() {
    const current = phaseRef.current;
    if (finishingRef.current || (current !== 'recording' && current !== 'paused')) return;
    const attempt = reconnectRef.current;
    if (attempt >= RECONNECT_DELAYS.length) {
      onToast('实时转写通道断开，已放弃重连', 'error');
      return;
    }
    reconnectRef.current = attempt + 1;
    onToast('实时转写通道断开，正在重连…');
    window.setTimeout(async () => {
      if (finishingRef.current) return;
      const meetingId = meetingIdRef.current;
      if (!meetingId) return;
      try {
        await openSocket(meetingId);
        reconnectRef.current = 0;
        // 断线期间在途块可能已发出但收不到确认：立即用同一 seq 重发（服务端幂等去重）。
        if (inflightRef.current) inflightRef.current.sent = false;
        pump();
        onToast('实时转写通道已恢复');
      } catch {
        scheduleReconnect();
      }
    }, RECONNECT_DELAYS[attempt]);
  }

  async function openSocket(meetingId: string) {
    await new Promise<void>((resolve, reject) => {
      const socket = new WebSocket(meetingWsUrl(meetingId));
      socketRef.current = socket;
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
          handleSocketMessage(JSON.parse(event.data), succeed);
        } catch {
          onToast('实时转写消息无法解析', 'error');
        }
      };
      socket.onclose = () => {
        if (socketRef.current === socket) socketRef.current = null;
        window.clearTimeout(timer);
        if (!settled) {
          settled = true;
          reject(new Error('实时转写通道已断开'));
          return;
        }
        // 仅在「连接曾建立后又断开」时重连；建连失败由调用方 catch 触发。
        if (opened) scheduleReconnect();
      };
    });
  }

  async function drain(timeoutMs: number) {
    const deadline = Date.now() + timeoutMs;
    while ((inflightRef.current || queueRef.current.length) && Date.now() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, 200));
    }
    return !inflightRef.current && queueRef.current.length === 0;
  }

  async function start() {
    if (!mossReady) {
      onToast(mossMessage, 'error');
      return;
    }
    setFailure(null);
    setPhase('starting');
    try {
      const created = await createMeeting({ mode, language, audio_source: source });
      setMeeting(created);
      if (mode === 'live') await openSocket(created.id);
      const capture = new MeetingCapture(source, mode === 'live', {
        onLevel: setLevel,
        onSpeechSegment: enqueue,
        onStreamEnded: async (reason) => {
          onToast(reason, 'error');
          if (captureRef.current) await finish();
        },
      });
      captureRef.current = capture;
      await capture.start();
      setPhase('recording');
      onToast(mode === 'live' ? '已开始实时转录' : '已开始录音');
    } catch (err) {
      captureRef.current = null;
      socketRef.current?.close();
      setPhase('idle');
      onToast((err as Error).message, 'error');
    }
  }

  async function finish() {
    const capture = captureRef.current;
    const current = meeting;
    if (!capture || !current) return;
    setPhase('stopping');
    finishingRef.current = true;
    try {
      // ① 先停止采集：capture.stop() 内部 flushSpeech(true) 会把最后不足一块的语音也入队。
      const file = await capture.stop();
      captureRef.current = null;
      // ② 等在途与队列全部转写完成，再发 WS stop。
      //    服务端 WS 循环是全串行的，「stop」排在最后一块 audio 之后才能保证它被处理入库。
      const drained = await drain(30000);
      if (!drained) {
        const left = queueRef.current.length + (inflightRef.current ? 1 : 0);
        onToast(`仍有 ${left} 段音频未转写完，已跳过`, 'error');
      }
      if (socketRef.current?.readyState === WebSocket.OPEN) {
        socketRef.current.send(JSON.stringify({ type: 'stop' }));
      }
      socketRef.current?.close();
      socketRef.current = null;
      // ③ 先无条件落盘音频（transcribe=false，与转写解耦），保证「停止即保存」。
      //    即使 MOSS 忙/未就绪，录音文件也已持久化，可稍后用重试按钮重跑转写。
      const saved = await uploadMeetingAudio(current.id, file, false);
      setMeeting(saved);
      if (saved.video) setVideo(saved.video);
      // ④ 落盘成功后再单独启动终稿转录。
      if (mossReady) {
        setPhase('processing');
        const next = await retryTranscribe(current.id);
        setMeeting(next);
        setJob(next.job || null);
        if (next.video) setVideo(next.video);
        onToast('录音已保存，正在生成终稿转录');
      } else {
        setFailure('录音已保存，但转录服务未就绪，可稍后点击「重试终稿转录」');
        setPhase('idle');
        onToast(mossMessage, 'error');
      }
    } catch (err) {
      // 音频可能已部分落盘；失败时不误报「没保存」，仅提示转写未完成。
      setFailure('录音可能未完整保存，请重试录制');
      setPhase('idle');
      onToast((err as Error).message, 'error');
    } finally {
      finishingRef.current = false;
    }
  }

  // 取消在途的终稿转录：后端会同时 kill 掉 MOSS 的推理子进程，立刻释放推理锁。
  async function cancelTranscription() {
    const currentJob = job;
    if (!currentJob) return;
    setCancelling(true);
    try {
      const updated = await cancelJob(currentJob.id);
      setJob(updated);
      setFailure('任务已取消，可以在下面重新开始终稿转录');
      setPhase('idle');
      onToast('已取消终稿转录');
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setCancelling(false);
    }
  }

  // 终稿转录失败/中断后的重试：录音文件还在，直接重跑，不必重新录制。
  async function retryFinalTranscript() {
    const current = meeting;
    if (!current) return;
    if (!mossReady) {
      onToast(mossMessage, 'error');
      return;
    }
    setRetrying(true);
    try {
      const next = await retryTranscribe(current.id);
      setMeeting(next);
      setJob(next.job || null);
      if (next.video) setVideo(next.video);
      setFailure(null);
      setPhase('processing');
      onToast('已重新启动终稿转录');
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setRetrying(false);
    }
  }

  async function importFile(file: File) {
    if (!mossReady) {
      onToast(mossMessage, 'error');
      return;
    }
    setFailure(null);
    setPhase('starting');
    try {
      const created = await createMeeting({ mode: 'recording', language, audio_source: source, title: file.name.replace(/\.[^.]+$/, '') });
      setMeeting(created);
      setPhase('processing');
      const next = await uploadMeetingAudio(created.id, file, true);
      setMeeting(next);
      setJob(next.job || null);
      if (next.video) setVideo(next.video);
      onToast('文件已导入，正在转录');
    } catch (err) {
      setPhase('idle');
      onToast((err as Error).message, 'error');
    }
  }

  const recording = phase === 'recording' || phase === 'paused';
  const locked = phase !== 'idle';
  const videoReady = Boolean(video && (phase === 'ready' || meeting?.status === 'ready'));

  return (
    <div className="meeting-workbench">
      <section className={`workspace-card meeting-card${phase === 'paused' ? ' is-paused' : ''}`}>
        <div className="mode-switch meeting-modes">
          <button type="button" className={mode === 'live' ? 'selected' : ''} disabled={locked} onClick={() => setMode('live')}>
            <strong>◎</strong>
            <span>实时转录<small>边录边出字，停录后生成终稿再总结</small></span>
          </button>
          <button type="button" className={mode === 'recording' ? 'selected' : ''} disabled={locked} onClick={() => setMode('recording')}>
            <strong>●</strong>
            <span>录音转录<small>先录完整段或导入文件，停止后整段转写</small></span>
          </button>
        </div>
        <div className="meeting-controls">
          <label>
            音源
            <select value={source} disabled={locked} onChange={(event) => setSource(event.target.value as AudioSource)}>
              <option value="mic">麦克风</option>
              <option value="system">系统音频（屏幕共享）</option>
              <option value="both">麦克风 + 系统音频</option>
            </select>
          </label>
          <label>
            语言
            <select value={language} disabled={locked} onChange={(event) => setLanguage(event.target.value)}>
              <option value="zh">中文</option>
              <option value="auto">自动识别</option>
              <option value="en">英语</option>
            </select>
          </label>
          <div className={`meeting-meter${phase === 'paused' ? ' paused' : ''}`}>
            <span>
              {formatClock(elapsed)}
              {phase === 'paused' ? <em className="pause-badge">已暂停</em> : null}
            </span>
            <i><b style={{ width: `${Math.round(level * 100)}%` }} /></i>
          </div>
        </div>
        <p className="meeting-hint">{sourceHint(source)}</p>
        {phase === 'paused' && (
          <p className="meeting-hint paused-hint">已暂停 · 暂停期间不采集音频，暂停时长不计入录音时长，点击「继续」接着录</p>
        )}
        <div className="meeting-actions">
          {phase === 'idle' ? (
            <button type="button" className="primary-button" onClick={start} disabled={!mossReady || busy}>开始</button>
          ) : null}
          {phase === 'ready' ? (
            <button type="button" className="outline-button" onClick={resetSession}>新开一场</button>
          ) : null}
          {recording ? (
            <>
              <button
                type="button"
                className={phase === 'paused' ? 'primary-button' : 'outline-button'}
                onClick={() => {
                  if (phase === 'paused') {
                    captureRef.current?.resume();
                    setPhase('recording');
                  } else {
                    captureRef.current?.pause();
                    setLevel(0);
                    setPhase('paused');
                  }
                }}
              >{phase === 'paused' ? '继续' : '暂停'}</button>
              <button type="button" className={phase === 'paused' ? 'outline-button' : 'primary-button'} onClick={finish}>停止</button>
            </>
          ) : null}
          {phase === 'starting' ? <button type="button" className="primary-button" disabled>正在启动…</button> : null}
          {phase === 'stopping' ? <button type="button" className="primary-button" disabled>正在保存…</button> : null}
          {phase === 'processing' ? (
            <>
              <button type="button" className="outline-button" disabled>终稿转录中…</button>
              {job ? (
                <button type="button" className="outline-button" onClick={cancelTranscription} disabled={cancelling}>
                  {cancelling ? '正在取消…' : '取消转录'}
                </button>
              ) : null}
            </>
          ) : null}
        </div>
        {!mossReady && <p className="inline-warning">{mossMessage}</p>}
      </section>

      {mode === 'live' && (recording || segments.length > 0) && (
        <section className="workspace-card live-transcript">
          <div className="workspace-heading">
            <div>
              <h2>实时转写</h2>
              <p>说话人标签在短片段上可能跳动，停录后的终稿会重新分离。</p>
            </div>
            {phase === 'paused' ? (
              <span className="live-status paused">已暂停</span>
            ) : (busyHint || lagSec > 5) ? (
              <span className="live-status">{busyHint || `转写滞后 ≈ ${lagSec}s`}</span>
            ) : null}
          </div>
          <div className="live-transcript-list" ref={listRef}>
            {segments.length === 0 && <p className="empty-live">正在听，开口后文字会出现在这里。</p>}
            {segments.map((item, index) => (
              <article key={`${item.seq || index}-${item.start_ms || 0}`}>
                <small>{item.speaker || '发言'} · {formatStamp(item.start_ms)}</small>
                <p>{item.text}</p>
              </article>
            ))}
          </div>
        </section>
      )}

      {mode === 'recording' && phase === 'idle' && (
        <section className="workspace-card import-card">
          <div className="workspace-heading">
            <div>
              <h2>导入音频或视频</h2>
              <p>也可以不现场录音，直接上传已有文件做整段转录。</p>
            </div>
            <span className="upload-limit">最大 500MB</span>
          </div>
          <div
            className="upload-dropzone"
            onClick={() => fileRef.current?.click()}
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => {
              event.preventDefault();
              const file = event.dataTransfer.files[0];
              if (file) importFile(file);
            }}
          >
            <input ref={fileRef} type="file" accept={ACCEPT} hidden onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = '';
              if (file) importFile(file);
            }} />
            <span className="upload-icon">↑</span>
            <strong>拖拽文件到这里，或点击选择</strong>
            <small>支持 MP3、WAV、M4A、MP4、WebM、MOV</small>
          </div>
        </section>
      )}

      {(phase === 'processing' || failure) && job && (
        <section className="workspace-card">
          <h2>终稿转录</h2>
          <SummaryProgress job={job} startLabel="开始转录" />
        </section>
      )}

      {failure && meeting && (
        <section className="workspace-card">
          <div className="workspace-heading">
            <div>
              <h2>终稿转录未完成</h2>
              <p>{failure}</p>
            </div>
            <button
              type="button"
              className="primary-button"
              onClick={retryFinalTranscript}
              disabled={retrying || !mossReady}
            >{retrying ? '正在重新启动…' : '重试终稿转录'}</button>
          </div>
          {!mossReady && <p className="inline-warning">{mossMessage}</p>}
        </section>
      )}

      {videoReady && video && (
        <ParsedVideoCard
          id="meeting-result"
          video={video}
          platforms={platforms}
          variants={video.variants || []}
          loading={false}
          initialPanel="summarize"
          meetingMode
          preferredTemplateName="标准会议纪要"
          onReparse={() => undefined}
          onToast={onToast}
        />
      )}
    </div>
  );
}
