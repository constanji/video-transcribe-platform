'use client';

import { useEffect, useRef } from 'react';
import type { Job } from '../lib/types';

function percent(job: Job) {
  if (job.status === 'completed') return 100;
  const detail = job.progress_detail?.percent;
  if (typeof detail === 'number') {
    return Math.max(0, Math.min(job.status === 'failed' || job.status === 'cancelled' ? detail : 99, detail));
  }
  const raw = Number(job.progress || 0);
  const value = raw <= 1 ? Math.round(raw * 100) : Math.round(raw);
  return Math.max(0, Math.min(job.status === 'failed' || job.status === 'cancelled' ? value : 99, value));
}

function clock(value?: string) {
  if (!value) return '--:--:--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(11, 19) || value;
  return date.toLocaleTimeString('zh-CN', { hour12: false });
}

function durationLabel(seconds?: number | null) {
  if (seconds == null || Number.isNaN(Number(seconds))) return '';
  const value = Math.max(0, Math.round(Number(seconds)));
  const minutes = Math.floor(value / 60);
  const rest = value % 60;
  return minutes ? `${minutes} 分 ${rest} 秒` : `${rest} 秒`;
}

export default function SummaryProgress({
  job,
  startLabel,
  startHint,
  showStart,
  startDisabled,
  onStart,
}: {
  job: Job | null;
  startLabel: string;
  startHint?: string;
  showStart?: boolean;
  startDisabled?: boolean;
  onStart?: () => void;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const logs = job?.logs || [];
  const failed = Boolean(job && (job.status === 'failed' || job.status === 'cancelled'));
  const done = job?.status === 'completed';
  const running = Boolean(job && !failed && !done);
  const detail = job?.progress_detail;
  const current = detail?.message || (logs.length ? logs[logs.length - 1].message : showStart ? `点击下方「${startLabel}」开始` : '选择上方「转录」或「总结」查看日志');
  const value = job ? percent(job) : 0;
  const meta = [
    detail?.audio_seconds ? `音频 ${durationLabel(detail.audio_seconds)}` : '',
    detail?.elapsed_seconds ? `已用 ${durationLabel(detail.elapsed_seconds)}` : '',
    running && detail?.eta_seconds != null ? `预计剩余 ${durationLabel(detail.eta_seconds)}` : '',
  ].filter(Boolean).join(' · ');

  useEffect(() => {
    const node = scroller.current;
    if (!node) return;
    node.scrollTop = node.scrollHeight;
  }, [logs.length, job?.status, current]);

  return (
    <div className={`summary-log ${!job ? 'idle' : failed ? 'failed' : done ? 'done' : 'running'}`}>
      <div className="summary-log-head">
        <div>
          <span>处理进度</span>
          <strong>{current}</strong>
          {meta && <small>{meta}</small>}
        </div>
        <em>{job ? `${value}%` : '待开始'}</em>
      </div>
      <div className="summary-progress-bar" aria-hidden>
        <i className={running && value < 8 ? 'indeterminate' : ''} style={{ width: `${Math.max(value, running ? 6 : 0)}%` }} />
      </div>
      <div className="summary-log-stream" ref={scroller}>
        {showStart && !logs.length && (
          <div className="summary-log-cta">
            <button type="button" className="primary-button" onClick={onStart} disabled={startDisabled}>
              {startLabel}
            </button>
            {startHint && <p>{startHint}</p>}
          </div>
        )}
        {!showStart && !job && <p className="summary-log-empty">还没有日志。选择上方「转录」或「总结」查看对应任务。</p>}
        {job && logs.length === 0 && !showStart && <p className="summary-log-empty">任务已创建，正在等待第一条日志…</p>}
        {logs.map((item, index) => (
          <div className={`summary-log-item ${item.level}`} key={`${item.at}-${index}`}>
            <time dateTime={item.at}>{clock(item.at)}</time>
            <p>{item.message}</p>
          </div>
        ))}
        {showStart && logs.length > 0 && (
          <div className="summary-log-retry">
            <button type="button" className="primary-button" onClick={onStart} disabled={startDisabled}>
              {startLabel}
            </button>
            {startHint && <p>{startHint}</p>}
          </div>
        )}
      </div>
    </div>
  );
}
