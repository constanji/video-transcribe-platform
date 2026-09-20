'use client';

import { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { api } from '../lib/api';
import type { Job, Summary, Transcript, VideoAsset } from '../lib/types';
import { cleanSummaryMarkdown } from '../lib/summary-text';

function Icon({ d, label }: { d: string; label?: string }) {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden={label ? undefined : true} aria-label={label}>
      <path fill="currentColor" d={d} />
    </svg>
  );
}

function segmentStart(item: { start?: number; start_ms?: number }) {
  if (typeof item.start_ms === 'number') return item.start_ms;
  if (typeof item.start === 'number') return item.start > 1000 ? item.start : item.start * 1000;
  return 0;
}

function clock(ms: number) {
  const sec = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(sec / 60);
  const rest = sec % 60;
  return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
}

export default function TranscriptStudio({
  videoId,
  templateId,
  templateName,
  summaryReady,
  activeJob,
  onClose,
  onToast,
  onJobChange,
}: {
  videoId: string;
  templateId: string;
  templateName?: string;
  summaryReady: boolean;
  activeJob?: Job | null;
  onClose: () => void;
  onToast: (message: string, kind?: 'success' | 'error') => void;
  onJobChange?: (job: Job) => void;
}) {
  const [video, setVideo] = useState<VideoAsset | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    const next = await api<VideoAsset>(`/api/videos/${videoId}`);
    setVideo(next);
    return next;
  }

  useEffect(() => {
    load().catch((err) => onToast((err as Error).message, 'error'));
  }, [videoId]);

  useEffect(() => {
    if (activeJob?.job_type === 'summarize') setJob(activeJob);
  }, [activeJob?.id, activeJob?.status, activeJob?.progress]);

  useEffect(() => {
    const body = document.body;
    const html = document.documentElement;
    const previousBody = body.style.overflow;
    const previousHtml = html.style.overflow;
    body.style.overflow = 'hidden';
    html.style.overflow = 'hidden';
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', onKey);
    return () => {
      body.style.overflow = previousBody;
      html.style.overflow = previousHtml;
      window.removeEventListener('keydown', onKey);
    };
  }, [onClose]);

  useEffect(() => {
    if (!job?.id || ['completed', 'failed', 'cancelled'].includes(job.status)) return;
    const timer = window.setInterval(async () => {
      try {
        const next = await api<Job>(`/api/jobs/${job.id}`);
        setJob(next);
        onJobChange?.(next);
        if (['completed', 'failed', 'cancelled'].includes(next.status)) {
          await load();
        }
      } catch {
        window.clearInterval(timer);
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [job?.id, job?.status]);

  const transcript: Transcript | undefined = video?.transcripts?.[0];
  const summary: Summary | undefined = video?.summaries?.[0];
  const summaryText = cleanSummaryMarkdown(summary?.content_markdown);
  const turns: Transcript['segments'] = useMemo(() => {
    const segments = transcript?.segments || [];
    if (segments.length) return segments.filter((item) => (item.text || '').trim());
    if (!transcript?.text) return [];
    return transcript.text.split('\n').filter(Boolean).map((text) => ({ text }));
  }, [transcript]);

  async function copyTranscript(withSpeaker = false) {
    if (!transcript?.text) return;
    const content = turns.length
      ? turns.map((item) => {
          const text = (item.text || '').trim();
          return withSpeaker && item.speaker ? `${item.speaker}：${text}` : text;
        }).filter(Boolean).join('\n')
      : transcript.text;
    await navigator.clipboard.writeText(content);
    onToast(withSpeaker ? '已复制转录（带说话人）' : '已复制转录内容');
  }

  async function generate() {
    if (!summaryReady) {
      window.location.href = '/settings?tab=transcription';
      return;
    }
    setBusy(true);
    try {
      const created = await api<Job>(`/api/videos/${videoId}/summaries`, {
        method: 'POST',
        body: JSON.stringify({ template_id: templateId }),
      });
      setJob(created);
      onJobChange?.(created);
      onToast('正在根据转录生成摘要');
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setBusy(false);
    }
  }

  const generating = Boolean(job && !['completed', 'failed', 'cancelled'].includes(job.status));
  const failed = job?.status === 'failed';
  const progress = Math.max(0, Math.min(100, Math.round((job?.progress || 0) * 100)));
  const dialog = (
    <div className="studio-mask" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <div className="studio-card" role="dialog" aria-modal="true" aria-label="转录与总结结果">
        <header className="studio-titlebar">
          <strong>{video?.title || '转录结果'}</strong>
          <button type="button" className="studio-close" onClick={onClose} aria-label="关闭">×</button>
        </header>
        <div className="studio-body">
          <section className="studio-pane">
            <div className="studio-pane-toolbar">
              <div className="copy-split">
                <button type="button" onClick={() => copyTranscript(false)} disabled={!transcript}>
                  <Icon d="M16 1H4c-1.1 0-2 .9-2 2v12h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z" />
                  复制
                </button>
                <details>
                  <summary aria-label="复制选项">⌄</summary>
                  <div className="copy-menu">
                    <button type="button" onClick={() => copyTranscript(false)}>仅复制内容</button>
                    <button type="button" onClick={() => copyTranscript(true)}>复制内容与说话人</button>
                  </div>
                </details>
              </div>
            </div>
          <div className="studio-turns">
            {!transcript && <p className="studio-empty-text">还没有转录结果。</p>}
            {turns.map((item, index) => (
              <article className="studio-turn" key={`${item.text}-${index}`}>
                <time>[{clock(segmentStart(item))}]</time>
                <div>
                  {item.speaker && <em>{item.speaker}</em>}
                  <p>{item.text}</p>
                </div>
              </article>
            ))}
          </div>
          </section>
          <section className="studio-pane">
            <div className="studio-pane-toolbar studio-summary-toolbar">
              <button
                type="button"
                className={summaryReady ? 'studio-tool active' : 'studio-tool muted-button'}
                data-tip={summaryReady ? undefined : '未配置总结模型'}
                onClick={generate}
                disabled={busy || generating || !transcript}
              >
                <Icon d="M12 2l1.6 4.4L18 8l-4.4 1.6L12 14l-1.6-4.4L6 8l4.4-1.6L12 2zm7 9l.9 2.6L23 15l-3.1.9L19 19l-.9-3.1L15 15l3.1-.9L19 11zM5 14l.8 2.2L8 17l-2.2.8L5 20l-.8-2.2L2 17l2.2-.8L5 14z" />
                {generating ? '生成中…' : summaryText ? '重新生成摘要' : '生成摘要'}
              </button>
              <label className="studio-tool studio-tool-select">
                <Icon d="M12.9 15h-1.8L9.6 11.4 8 15H6.2l2.8-7h1.6l2.3 7zM4 4h16v2H4V4zm2 14h12v2H6v-2z" />
                <select defaultValue="zh-CN" aria-label="摘要语言">
                  <option value="zh-CN">简体中文</option>
                </select>
              </label>
              <button
                type="button"
                className="studio-tool"
                title="配置总结模型"
                onClick={() => { window.location.href = '/settings?tab=transcription'; }}
              >
                <Icon d="M12 2a4 4 0 014 4v1h1a3 3 0 013 3v2h-2v5a3 3 0 01-3 3H9a3 3 0 01-3-3v-5H4V10a3 3 0 013-3h1V6a4 4 0 014-4zm-1 14v3h2v-3h-2zm0-8v6h2V8h-2z" />
                AI 模型
              </button>
              <button type="button" className="studio-tool" title={templateName || '当前总结模板'}>
                <Icon d="M6 2h9l5 5v13a2 2 0 01-2 2H6a2 2 0 01-2-2V4a2 2 0 012-2zm8 1.5V8h4.5L14 3.5zM8 12h8v2H8v-2zm0 4h8v2H8v-2z" />
                模板
              </button>
            </div>
            {generating ? (
              <div className="studio-empty studio-processing">
                <span className="studio-spinner" />
                <h3>正在生成摘要</h3>
                <p>{job?.progress_detail?.message || '模型正在读取转录内容，请稍候。'}</p>
                <div className="studio-progress"><i style={{ width: `${Math.max(progress, 8)}%` }} /></div>
                <small>{progress}%</small>
              </div>
            ) : failed ? (
              <div className="studio-empty studio-failed">
                <span className="studio-empty-icon">!</span>
                <h3>摘要生成失败</h3>
                <p>{job?.error_message || '生成摘要时发生错误，请重试。'}</p>
                <button type="button" className="primary-button" onClick={generate}>重新生成摘要</button>
              </div>
            ) : summaryText ? (
            <article className="studio-summary markdown-body">{summaryText}</article>
          ) : summary ? (
            <div className="studio-empty">
              <span className="studio-empty-icon">?</span>
              <h3>这次没有可用总结</h3>
              <p>模型内部过程已过滤。请重新生成摘要。</p>
              <button
                type="button"
                className={summaryReady ? 'primary-button' : 'muted-button'}
                data-tip={summaryReady ? undefined : '未配置总结模型'}
                onClick={generate}
                disabled={busy || generating || !transcript}
              >
                {generating ? '生成中…' : '重新生成摘要'}
              </button>
            </div>
          ) : (
            <div className="studio-empty">
              <span className="studio-empty-icon">?</span>
              <h3>尚未生成摘要</h3>
              <p>根据会议转录生成摘要，提取要点、行动项和结论。</p>
              <button
                type="button"
                className={summaryReady ? 'primary-button' : 'muted-button'}
                data-tip={summaryReady ? undefined : '未配置总结模型'}
                onClick={generate}
                disabled={busy || generating || !transcript}
              >
                {generating ? '生成中…' : '生成摘要'}
              </button>
            </div>
          )}
          </section>
        </div>
      </div>
    </div>
  );
  if (typeof document === 'undefined') return dialog;
  return createPortal(dialog, document.body);
}
