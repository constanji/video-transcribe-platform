'use client';

import { useEffect, useRef } from 'react';
import { MAX_FILE_BYTES, meetingSession, useMeetingSession, type MeetingMode } from '../lib/meeting-session';
import type { AudioSource } from '../lib/meeting-capture';
import type { Platform, Template } from '../lib/types';
import ParsedVideoCard from './ParsedVideoCard';
import SummaryProgress from './SummaryProgress';

const ACCEPT = '.mp3,.wav,.m4a,.mp4,.webm,.mov';

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

/**
 * 会议工作台：纯 UI 视图。录音/转写运行时在 lib/meeting-session.ts 的全局单例里，
 * 切到其他页面（客户端路由）后会话继续在后台跑，由悬浮条接管控制。
 */
export default function MeetingWorkbench({
  platforms,
  templates,
  templateId,
  onToast,
}: {
  platforms: Platform[];
  templates: Template[];
  templateId: string;
  onToast: (message: string, kind?: 'success' | 'error') => void;
}) {
  const state = useMeetingSession();
  const {
    mode, phase, source, language, level, elapsed, segments, meeting, video, job,
    busyHint, failure, lagSec, uploadProgress, cancelling, retrying, mossReady, mossMessage,
  } = state;

  // 会话动作产生的提示统一走单例的 toast 通道（悬浮条在别的页面时也能收到）。
  useEffect(() => {
    meetingSession.setToastHandler(onToast);
    return () => meetingSession.setToastHandler(null);
  }, [onToast]);

  const listRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' });
  }, [segments.length]);

  const recording = phase === 'recording' || phase === 'paused';
  const busy = phase === 'starting' || phase === 'stopping';
  const locked = phase !== 'idle';
  const videoReady = Boolean(video && (phase === 'ready' || meeting?.status === 'ready'));
  const preferredTemplate = templates.find((item) => item.id === templateId)?.name;

  return (
    <div className="meeting-workbench">
      <section className={`workspace-card meeting-card${phase === 'paused' ? ' is-paused' : ''}`}>
        <div className="mode-switch meeting-modes">
          <button type="button" className={mode === 'live' ? 'selected' : ''} disabled={locked} onClick={() => meetingSession.setMode('live' as MeetingMode)}>
            <strong>◎</strong>
            <span>实时转录<small>边录边出字，停录后生成终稿再总结</small></span>
          </button>
          <button type="button" className={mode === 'recording' ? 'selected' : ''} disabled={locked} onClick={() => meetingSession.setMode('recording' as MeetingMode)}>
            <strong>●</strong>
            <span>录音转录<small>先录完整段或导入文件，停止后整段转写</small></span>
          </button>
        </div>
        <div className="meeting-controls">
          <label>
            音源
            <select value={source} disabled={locked} onChange={(event) => meetingSession.setSource(event.target.value as AudioSource)}>
              <option value="mic">麦克风</option>
              <option value="system">系统音频（屏幕共享）</option>
              <option value="both">麦克风 + 系统音频</option>
            </select>
          </label>
          <label>
            语言
            <select value={language} disabled={locked} onChange={(event) => meetingSession.setLanguage(event.target.value)}>
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
            <button type="button" className="primary-button" onClick={() => meetingSession.start()} disabled={!mossReady || busy}>开始</button>
          ) : null}
          {phase === 'ready' ? (
            <button type="button" className="outline-button" onClick={() => meetingSession.resetSession()}>新开一场</button>
          ) : null}
          {recording ? (
            <>
              <button
                type="button"
                className={phase === 'paused' ? 'primary-button' : 'outline-button'}
                onClick={() => meetingSession.togglePause()}
              >{phase === 'paused' ? '继续' : '暂停'}</button>
              <button type="button" className={phase === 'paused' ? 'outline-button' : 'primary-button'} onClick={() => meetingSession.finish()}>停止</button>
            </>
          ) : null}
          {phase === 'starting' ? <button type="button" className="primary-button" disabled>正在启动…</button> : null}
          {phase === 'stopping' ? <button type="button" className="primary-button" disabled>正在保存…</button> : null}
          {phase === 'uploading' ? (
            <>
              <button type="button" className="primary-button" disabled>正在上传… {uploadProgress ?? 0}%</button>
              <button type="button" className="outline-button" onClick={() => meetingSession.abortUpload()}>取消上传</button>
            </>
          ) : null}
          {phase === 'processing' ? (
            <>
              <button type="button" className="outline-button" disabled>终稿转录中…</button>
              {job ? (
                <button type="button" className="outline-button" onClick={() => meetingSession.cancelTranscription()} disabled={cancelling}>
                  {cancelling ? '正在取消…' : '取消转录'}
                </button>
              ) : null}
            </>
          ) : null}
        </div>
        {!mossReady && <p className="inline-warning">{mossMessage}</p>}
        {uploadProgress !== null && (
          <div className="studio-progress" role="progressbar" aria-label="录音上传进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={uploadProgress}>
            <i style={{ width: `${uploadProgress}%` }} />
          </div>
        )}
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
            ) : recording ? (
              // MOSS 在 CPU 上按块转写（16s 块约需 14s），说完等十几秒才出字是正常速度，
              // 不明说会被当成「不实时」的故障。
              <span className="live-status is-hint">CPU 转写 · 字幕滞后约 10~20 秒</span>
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
            role="button"
            tabIndex={0}
            aria-label="选择或拖入音频视频文件"
            onClick={() => fileRef.current?.click()}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                fileRef.current?.click();
              }
            }}
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => {
              event.preventDefault();
              const file = event.dataTransfer.files[0];
              if (file) meetingSession.importFile(file);
            }}
          >
            <input ref={fileRef} type="file" accept={ACCEPT} hidden onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = '';
              if (file) meetingSession.importFile(file);
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
              onClick={() => meetingSession.retryFinalTranscript()}
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
          meetingId={meeting?.id}
          platforms={platforms}
          variants={video.variants || []}
          loading={false}
          initialPanel="summarize"
          meetingMode
          preferredTemplateName={preferredTemplate}
          onReparse={() => undefined}
          onToast={onToast}
        />
      )}
    </div>
  );
}
