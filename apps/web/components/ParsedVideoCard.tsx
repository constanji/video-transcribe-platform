'use client';

import { useCallback, useEffect, useState } from 'react';
import { api, logoSrc } from '../lib/api';
import { withToken } from '../lib/auth';
import type { Job, MediaVariant, Platform, VideoAsset } from '../lib/types';
import DownloadAction from './DownloadAction';
import ImageGallery from './ImageGallery';
import SummaryAction from './SummaryAction';
import SummaryProgress from './SummaryProgress';

type AnalyzePanel = 'transcribe' | 'summarize';
type CardPanel = 'download' | AnalyzePanel | null;

function formatDuration(seconds?: number | null) {
  if (!seconds) return '时长待获取';
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (!minutes) return `${rest} 秒`;
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`;
}

function latestJob(video: VideoAsset, type: 'transcribe' | 'summarize'): Job | null {
  return [...(video.jobs || [])]
    .filter((item) => item.job_type === type)
    .sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at))[0] || null;
}

function isRunning(job?: Job | null) {
  return Boolean(job && !['completed', 'failed', 'cancelled'].includes(job.status));
}

function hasUsableTranscript(video: VideoAsset) {
  return Boolean(video.transcripts?.some((item) => (item.text || '').trim()));
}

function hasUsableSummary(video: VideoAsset) {
  return Boolean(video.summaries?.some((item) => (item.content_markdown || '').trim()));
}

function normalizePanel(panel?: 'download' | 'summary' | AnalyzePanel | null): CardPanel {
  if (panel === 'summary') return 'transcribe';
  return panel || null;
}

export default function ParsedVideoCard({
  video,
  platforms,
  variants,
  id,
  loading,
  initialPanel = null,
  meetingMode = false,
  meetingId,
  preferredTemplateName,
  onReparse,
  onToast,
  onPanelChange,
  onClose,
}: {
  video: VideoAsset;
  platforms: Platform[];
  variants: MediaVariant[];
  id?: string;
  loading: boolean;
  initialPanel?: 'download' | 'summary' | AnalyzePanel | null;
  meetingMode?: boolean;
  meetingId?: string;
  preferredTemplateName?: string;
  onReparse: () => void;
  onToast: (message: string, kind?: 'success' | 'error') => void;
  onPanelChange?: (panel: 'download' | 'summary' | AnalyzePanel | null) => void;
  onClose?: () => void;
}) {
  const [panel, setPanel] = useState<CardPanel>(normalizePanel(initialPanel));
  const [transcribeJob, setTranscribeJob] = useState<Job | null>(latestJob(video, 'transcribe'));
  const [summarizeJob, setSummarizeJob] = useState<Job | null>(latestJob(video, 'summarize'));
  const [templateId, setTemplateId] = useState('');
  const [summaryReady, setSummaryReady] = useState(true);
  const [hasTranscript, setHasTranscript] = useState(Boolean(video.has_transcript));
  const [hasSummary, setHasSummary] = useState(Boolean(video.has_summary));
  const [busy, setBusy] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [language, setLanguage] = useState('zh');
  const [mossReady, setMossReady] = useState(false);
  const [mossMessage, setMossMessage] = useState('正在检查转录服务…');
  const platform = platforms.find((item) => item.id === video.platform);
  const analyzeOpen = panel === 'transcribe' || panel === 'summarize';
  const transcribeRunning = isRunning(transcribeJob);
  const summarizeRunning = isRunning(summarizeJob);

  useEffect(() => {
    setPanel(normalizePanel(initialPanel));
  }, [video.id, initialPanel]);

  useEffect(() => {
    setTranscribeJob(latestJob(video, 'transcribe'));
    setSummarizeJob(latestJob(video, 'summarize'));
    setHasTranscript(hasUsableTranscript(video));
    setHasSummary(hasUsableSummary(video));
  }, [video.id, video.last_processed_at, video.has_transcript, video.has_summary]);

  useEffect(() => {
    api<{ default_language?: string }>('/api/settings/transcription').then((settings) => {
      if (settings.default_language) setLanguage(settings.default_language);
    }).catch(() => undefined);
  }, [video.id]);

  useEffect(() => {
    let active = true;
    async function checkMoss() {
      try {
        const health = await api<{ moss_reachable?: boolean; moss_model_loaded?: boolean; moss_model_loading?: boolean; moss_warmup_error?: string | null }>('/api/system/health');
        if (!active) return;
        const ready = Boolean(health.moss_reachable && health.moss_model_loaded);
        setMossReady(ready);
        setMossMessage(ready ? '' : !health.moss_reachable ? '转录服务未连接，请启动本机 MOSS' : health.moss_model_loading ? 'MOSS 模型加载中，请等待就绪' : health.moss_warmup_error || 'MOSS 转录模型尚未加载');
      } catch {
        if (active) { setMossReady(false); setMossMessage('无法检查转录服务，请确认本机 API 正常'); }
      }
    }
    void checkMoss();
    const timer = window.setInterval(checkMoss, 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [video.id]);

  useEffect(() => {
    const current = panel === 'summarize' ? summarizeJob : panel === 'transcribe' ? transcribeJob : null;
    if (!current?.id || (current.logs && current.logs.length)) return;
    api<Job>(`/api/jobs/${current.id}`).then((next) => {
      if (next.job_type === 'summarize') setSummarizeJob(next);
      else setTranscribeJob(next);
    }).catch(() => undefined);
  }, [panel, transcribeJob?.id, summarizeJob?.id]);

  function changePanel(next: CardPanel) {
    setPanel(next);
    onPanelChange?.(next);
  }

  function switchAnalyze(next: AnalyzePanel) {
    changePanel(next);
  }

  async function start(kind: AnalyzePanel) {
    if (kind === 'summarize' && !summaryReady) {
      window.location.href = '/settings?tab=transcription';
      return;
    }
    if (kind === 'summarize' && !templateId) return;
    if (kind === 'summarize' && !hasTranscript && !mossReady) {
      onToast(mossMessage || '转录服务尚未就绪', 'error');
      return;
    }
    setBusy(true);
    changePanel(kind);
    try {
      const path = kind === 'transcribe' ? `/api/videos/${video.id}/transcribe` : `/api/videos/${video.id}/summary`;
      const created = await api<Job>(path, {
        method: 'POST',
        body: JSON.stringify({ video_id: video.id, template_id: templateId, reuse_transcript: true, transcription: { language } }),
      });
      if (created.job_type === 'summarize') setSummarizeJob(created);
      else setTranscribeJob(created);
      onToast(kind === 'transcribe' ? '转录已开始，进度见右侧日志' : '正在按模板转录并总结');
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setBusy(false);
    }
  }

  async function cancel(job: Job | null) {
    if (!job?.id || cancelling) return;
    setCancelling(true);
    try {
      const updated = await api<Job>(`/api/jobs/${job.id}`, { method: 'DELETE' });
      if (updated.job_type === 'summarize') setSummarizeJob(updated);
      else setTranscribeJob(updated);
      onToast('任务已取消');
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setCancelling(false);
    }
  }

  const takeJob = useCallback((job: Job | null) => {
    if (!job) return;
    if (job.job_type === 'summarize') setSummarizeJob(job);
    if (job.job_type === 'transcribe') setTranscribeJob(job);
  }, []);

  const handleResultState = useCallback(({ transcript, summary }: { transcript: boolean; summary: boolean }) => {
    setHasTranscript((current) => current === transcript ? current : transcript);
    setHasSummary((current) => current === summary ? current : summary);
  }, []);

  const handleSummaryReady = useCallback((ready: boolean) => {
    setSummaryReady((current) => current === ready ? current : ready);
  }, []);

  const images = variants.filter((item) => item.media_type === 'image');
  const imageMode = !meetingMode && images.length > 0 && !variants.some((item) => item.media_type === 'video' || item.media_type === 'audio');
  if (imageMode && video.id) {
    return (
      <ImageGallery
        id={id}
        video={video}
        platform={platform}
        images={images}
        onReparse={() => { setTranscribeJob(null); setSummarizeJob(null); onReparse(); }}
        onToast={onToast}
      />
    );
  }

  if (loading) {
    return (
      <section className="result-card skeleton-card" aria-busy="true">
        {onClose && <button type="button" className="result-close" aria-label="关闭操作台" title="关闭" onClick={onClose}>×</button>}
        <div className="result-side">
          <div className="result-cover skeleton" />
        </div>
        <div className="result-content">
          <div className="skeleton line w40" />
          <div className="skeleton line w80" />
          <div className="skeleton line w60" />
          <p className="meta-line">正在识别平台并提取媒体信息…</p>
        </div>
      </section>
    );
  }

  const currentJob = panel === 'summarize' ? summarizeJob : transcribeJob;
  const transcribeDone = Boolean(hasTranscript || transcribeJob?.status === 'completed');
  const summaryDone = Boolean(hasSummary || summarizeJob?.status === 'completed');
  const transcribeFailed = Boolean(transcribeJob && ['failed', 'cancelled'].includes(transcribeJob.status));
  const summaryFailed = Boolean(summarizeJob && ['failed', 'cancelled'].includes(summarizeJob.status));
  const transcribeStart = !transcribeRunning && (!transcribeDone || transcribeFailed);
  const summaryStart = !summarizeRunning && (!summaryDone || summaryFailed);
  const blockedHint = transcribeRunning && panel === 'summarize'
    ? '转录进行中，完成后可开始总结'
    : summarizeRunning && panel === 'transcribe'
      ? '总结进行中，完成后可开始转录'
      : panel === 'summarize' && !transcribeDone && !mossReady
        ? mossMessage
      : panel === 'summarize' && !transcribeDone
        ? '还没有完成的转录，开始后会先转录再总结'
        : undefined;

  return (
    <section id={id} className={`result-card ${panel ? 'is-open' : ''} ${analyzeOpen ? 'is-summary' : ''} ${panel === 'download' ? 'is-download' : ''}`}>
      <div className="result-side">
        <div className="result-cover">
          {video.thumbnail_url ? <img src={withToken(video.thumbnail_url)} alt="视频封面" /> : (
            <div className="cover-placeholder">
              <span>◈</span>
              <small>封面将在解析器返回后显示</small>
            </div>
          )}
        </div>
        <div className="result-side-extra">
          <div className="result-side-extra-inner">
            <SummaryAction
              videoId={video.id}
              meetingId={meetingId}
              resetKey={video.last_processed_at || video.id}
              initialJob={currentJob}
              hasTranscript={hasTranscript}
              hasSummary={hasSummary}
              showLaunch={false}
              preferredTemplateName={preferredTemplateName}
              listedTemplates={meetingMode ? false : true}
              onToast={onToast}
              onJob={takeJob}
              onTemplateId={setTemplateId}
              onSummaryReady={handleSummaryReady}
              onResultState={handleResultState}
            />
          </div>
        </div>
      </div>
      <div className="result-content">
        <div className="result-top">
          <span className="platform-badge">
            <img src={logoSrc(platform?.logo)} alt="" width={16} height={16} />
            {platform?.name || video.platform || '待识别平台'}
          </span>
          <div className="result-top-right">
            <span className={`status-text ${video.parse_status}`}>{video.parse_status === 'uploaded' ? '文件已就绪' : video.parse_status === 'parsed' ? '解析完成' : '等待解析器接入'}</span>
            {video.parse_status !== 'uploaded' && <button type="button" className="text-button" onClick={() => { setTranscribeJob(null); setSummarizeJob(null); onReparse(); }}>重新解析</button>}
            {onClose && <button type="button" className="result-close" aria-label="关闭操作台" title="关闭" onClick={onClose}>×</button>}
          </div>
        </div>
        <h2>{video.title || '待获取视频标题'}</h2>
        <p className="meta-line">
          {video.author || '作者信息待获取'}
          <i />
          {formatDuration(video.duration_seconds)}
        </p>
        {video.source_url && video.parse_status !== 'uploaded' && (
          <a className="source-link" href={video.source_url} target="_blank" rel="noreferrer">
            {video.source_url}
          </a>
        )}
        <div className="result-actions">
          {!meetingMode && (
            <button type="button" className={panel === 'download' ? 'primary-button' : 'outline-button'} onClick={() => changePanel(panel === 'download' ? null : 'download')}>
              视频下载
            </button>
          )}
          {!meetingMode && (
            <button
              type="button"
              className={panel === 'transcribe' ? 'primary-button' : 'outline-button'}
              onClick={() => switchAnalyze('transcribe')}
            >
              {transcribeRunning ? '转录中…' : '转录'}
            </button>
          )}
          <button
            type="button"
            className={summaryReady ? (panel === 'summarize' ? 'primary-button' : 'outline-button') : 'muted-button'}
            data-tip={summaryReady ? undefined : '未配置总结模型'}
            onClick={() => switchAnalyze('summarize')}
          >
            {summarizeRunning ? '总结中…' : '总结'}
          </button>
        </div>
        <div className="result-panels">
          <div className="result-panels-inner">
            <div className={`result-panel ${panel === 'download' ? 'is-active' : ''}`}>
              <DownloadAction video={video} variants={variants} onToast={onToast} />
            </div>
            <div className={`result-panel ${panel === 'transcribe' ? 'is-active' : ''}`}>
              <label className="transcription-language">转录语言<select value={language} onChange={(event) => setLanguage(event.target.value)}><option value="zh">中文</option><option value="auto">自动识别</option><option value="en">英语</option></select></label>
              <SummaryProgress
                job={transcribeJob}
                startLabel={transcribeJob?.status === 'failed' || transcribeJob?.status === 'cancelled' ? '重新转录' : '开始转录'}
                startHint={blockedHint || mossMessage}
                showStart={transcribeStart}
                startDisabled={busy || transcribeRunning || summarizeRunning || !mossReady}
                onStart={() => start('transcribe')}
                onCancel={() => cancel(transcribeJob)}
                cancelDisabled={cancelling}
              />
            </div>
            <div className={`result-panel ${panel === 'summarize' ? 'is-active' : ''}`}>
              <SummaryProgress
                job={summarizeJob}
                startLabel={summarizeJob?.status === 'failed' || summarizeJob?.status === 'cancelled' ? '重新总结' : '开始总结'}
                startHint={blockedHint}
                showStart={summaryStart}
                startDisabled={busy || transcribeRunning || summarizeRunning || (!transcribeDone && !mossReady)}
                onStart={() => start('summarize')}
                onCancel={() => cancel(summarizeJob)}
                cancelDisabled={cancelling}
              />
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
