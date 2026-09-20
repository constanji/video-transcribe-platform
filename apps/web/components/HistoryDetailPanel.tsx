'use client';

import { useState } from 'react';
import { logoSrc } from '../lib/api';
import type { Platform, Template, VideoAsset } from '../lib/types';
import DownloadAction from './DownloadAction';
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
  onRetry,
  onToast,
  onDeleted,
}: {
  video?: VideoAsset;
  platforms: Platform[];
  templates: Template[];
  onRetry: (jobId: string) => void;
  onToast: (message: string, kind?: 'success' | 'error') => void;
  onDeleted: (id: string) => void;
}) {
  const [tab, setTab] = useState<'summary' | 'transcript' | 'timeline'>('summary');
  const [confirm, setConfirm] = useState(false);
  const [showDownload, setShowDownload] = useState(false);
  const [showSummary, setShowSummary] = useState(false);
  if (!video) return <div className="empty-state">选择左侧任务查看详情</div>;
  const platform = platforms.find((item) => item.id === video.platform);

  return (
    <>
      <div className="detail-hero">
        <div className="detail-cover">{video.thumbnail_url ? <img src={video.thumbnail_url} alt="" /> : <span>◈</span>}</div>
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
      {showSummary && <SummaryAction videoId={video.id} hasTranscript={Boolean(video.has_transcript)} hasSummary={Boolean(video.has_summary)} onToast={onToast} />}
      <div className="detail-tabs">
        <button type="button" className={tab === 'summary' ? 'active' : ''} onClick={() => setTab('summary')}>总结</button>
        <button type="button" className={tab === 'transcript' ? 'active' : ''} onClick={() => setTab('transcript')}>转录</button>
        <button type="button" className={tab === 'timeline' ? 'active' : ''} onClick={() => setTab('timeline')}>处理时间线</button>
      </div>
      {tab === 'summary' && (
        <SummaryViewer summaries={video.summaries || []} templates={templates} onRetry={() => setShowSummary(true)} onToast={onToast} />
      )}
      {tab === 'transcript' && <TranscriptViewer transcript={video.transcripts?.[0]} />}
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
      {confirm && (
        <div className="confirm-mask">
          <div className="confirm-box">
            <h3>删除这个视频资产？</h3>
            <p>将同时删除其下载、转录和总结记录。此操作不可恢复。</p>
            <div className="detail-actions">
              <button type="button" className="primary-button" onClick={() => { onDeleted(video.id); setConfirm(false); }}>确认删除</button>
              <button type="button" className="outline-button" onClick={() => setConfirm(false)}>取消</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
