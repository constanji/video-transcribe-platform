'use client';

import { useEffect, useState } from 'react';
import { logoSrc } from '../lib/api';
import { withToken } from '../lib/auth';
import { isImageRecord } from '../lib/media-kind';
import { writeWorkbenchDraft } from '../lib/workbench-draft';
import type { Platform, Template, VideoAsset } from '../lib/types';
import DownloadAction from './DownloadAction';
import ImageGallery from './ImageGallery';
import SummaryAction from './SummaryAction';
import SummaryViewer from './SummaryViewer';
import TranscriptViewer from './TranscriptViewer';

const STAGE_ORDER = ['parse', 'download', 'extract', 'transcribe', 'summarize'];
const STAGE_LABEL: Record<string, string> = {
  parse: '解析',
  download: '下载',
  extract: '音频提取',
  transcribe: '转录',
  summarize: '总结',
};

export default function HistoryDetailPanel({
  video,
  platforms,
  templates,
  deleting,
  onRetry,
  onRefresh,
  onToast,
  onDeleted,
}: {
  video?: VideoAsset;
  platforms: Platform[];
  templates: Template[];
  deleting?: boolean;
  onRetry: (jobId: string) => void;
  onRefresh?: () => void;
  onToast: (message: string, kind?: 'success' | 'error') => void;
  onDeleted: (id: string) => void;
}) {
  const [tab, setTab] = useState<'summary' | 'transcript' | 'timeline'>('summary');
  const [confirm, setConfirm] = useState(false);
  const [awaitingDelete, setAwaitingDelete] = useState(false);
  const [showDownload, setShowDownload] = useState(false);
  const [showSummary, setShowSummary] = useState(false);
  useEffect(() => {
    if (!confirm) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !deleting && !awaitingDelete) setConfirm(false);
    };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [confirm, deleting, awaitingDelete]);
  useEffect(() => {
    // 点过确认后，删除结束（成功或失败）都关掉确认框；结果由 toast 反馈。
    if (awaitingDelete && !deleting) {
      setAwaitingDelete(false);
      setConfirm(false);
    }
  }, [awaitingDelete, deleting]);
  if (!video) return <div className="empty-state">选择左侧任务查看详情</div>;
  const platform = platforms.find((item) => item.id === video.platform);
  const confirmDialog = confirm && (
    <div className="confirm-mask">
      <div className="confirm-box" role="dialog" aria-modal="true" aria-labelledby="delete-record-title">
        <h3 id="delete-record-title">删除这个解析记录？</h3>
        <p>将同时删除其下载、转录和总结记录。此操作不可恢复。</p>
        <div className="detail-actions">
          <button type="button" className="primary-button" disabled={deleting || awaitingDelete} onClick={() => { setAwaitingDelete(true); onDeleted(video.id); }}>
            {deleting || awaitingDelete ? <><i className="badge-dot" />删除中…</> : '确认删除'}
          </button>
          <button type="button" className="outline-button" onClick={() => setConfirm(false)}>取消</button>
        </div>
      </div>
    </div>
  );

  if (isImageRecord(video)) {
    const images = (video.variants || []).filter((item) => item.media_type === 'image');
    return (
      <>
        <ImageGallery
          video={video}
          platform={platform}
          images={images}
          onReparse={() => {
            writeWorkbenchDraft({ url: video.source_url, videoId: video.id, panel: null });
            window.location.href = '/parse';
          }}
          onToast={onToast}
        />
        <div className="detail-actions">
          <button type="button" className="text-button" onClick={() => setConfirm(true)}>删除任务</button>
        </div>
        {confirmDialog}
      </>
    );
  }

  return (
    <>
      <div className="detail-hero">
        <div className="detail-cover">{video.thumbnail_url ? <img src={withToken(video.thumbnail_url)} alt="" /> : <span>◈</span>}</div>
        <div>
          <span className="platform-badge">
            <img src={logoSrc(platform?.logo)} alt="" width={16} height={16} />
            {platform?.name || video.platform || '未知平台'}
          </span>
          <h2>{video.title}</h2>
          <p>{video.author || '作者信息待获取'} · {video.duration_seconds ? `${Math.round(video.duration_seconds / 60)} 分钟` : '时长待获取'}</p>
          <p className="source-link">{video.source_url}</p>
          <div className="detail-actions">
            <button type="button" className="primary-button" onClick={() => setShowDownload((v) => !v)}>视频下载 →</button>
            <button type="button" className="outline-button" onClick={() => { setShowSummary(true); setTab('summary'); }}>重新总结</button>
            <button type="button" className="text-button" onClick={() => setConfirm(true)}>删除任务</button>
          </div>
        </div>
      </div>
      {showDownload && <DownloadAction video={video} variants={video.variants || []} onToast={onToast} />}
      {showSummary && (
        <SummaryAction
          videoId={video.id}
          hasTranscript={Boolean(video.has_transcript)}
          hasSummary={Boolean(video.has_summary)}
          onToast={onToast}
          onJob={(job) => {
            if (job && ['completed', 'failed', 'cancelled'].includes(job.status)) onRefresh?.();
          }}
        />
      )}
      <div className="detail-tabs">
        <button type="button" className={tab === 'summary' ? 'active' : ''} onClick={() => setTab('summary')}>总结</button>
        <button type="button" className={tab === 'transcript' ? 'active' : ''} onClick={() => setTab('transcript')}>转录</button>
        <button type="button" className={tab === 'timeline' ? 'active' : ''} onClick={() => setTab('timeline')}>处理时间线</button>
      </div>
      {tab === 'summary' && (
        <SummaryViewer summaries={video.summaries || []} templates={templates} onRetry={() => setShowSummary(true)} onToast={onToast} />
      )}
      {tab === 'transcript' && <TranscriptViewer transcript={video.transcripts?.[0]} onToast={onToast} />}
      {tab === 'timeline' && (
        <ol className="process-timeline">
          {STAGE_ORDER.map((stage) => {
            const job = (video.jobs || []).find((item) => item.job_type === stage || (stage === 'extract' && item.current_stage?.includes('audio')));
            return (
              <li key={stage}>
                <strong>{STAGE_LABEL[stage]}</strong>
                <span>状态：{job?.status || '未开始'}</span>
                <span>开始：{job?.started_at || job?.created_at || '—'}</span>
                <span>完成：{job?.finished_at || '—'}</span>
                {job?.error_message && <span className="inline-error">错误：{job.error_message}</span>}
                {job && job.status === 'failed' && <button type="button" className="text-button" onClick={() => onRetry(job.id)}>重试</button>}
              </li>
            );
          })}
        </ol>
      )}
      {confirmDialog}
    </>
  );
}
