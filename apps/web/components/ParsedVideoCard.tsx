'use client';

import { useEffect, useState } from 'react';
import { api, logoSrc } from '../lib/api';
import type { Job, MediaVariant, Platform, VideoAsset } from '../lib/types';
import DownloadAction from './DownloadAction';
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
  const stamp = video.last_processed_at;
  return (video.jobs || []).find((item) => {
    if (item.job_type !== type) return false;
    if (!stamp) return true;
    return item.created_at >= stamp;
  }) || null;
}

function isRunning(job?: Job | null) {
  return Boolean(job && !['completed', 'failed', 'cancelled'].includes(job.status));
}

function normalizePanel(panel?: 'download' | 'summary' | AnalyzePanel | null): CardPanel {
  if (panel === 'summary') return 'transcribe';
  return panel || null;
}

export default function ParsedVideoCard({
  video,
  platforms,
  variants,
  loading,
  initialPanel = null,
  onReparse,
  onToast,
  onPanelChange,
}: {
  video: VideoAsset;
  platforms: Platform[];
  variants: MediaVariant[];
  loading: boolean;
  initialPanel?: 'download' | 'summary' | AnalyzePanel | null;
  onReparse: () => void;
  onToast: (message: string, kind?: 'success' | 'error') => void;
  onPanelChange?: (panel: 'download' | 'summary' | AnalyzePanel | null) => void;
}) {
  const [panel, setPanel] = useState<CardPanel>(normalizePanel(initialPanel));
  const [transcribeJob, setTranscribeJob] = useState<Job | null>(latestJob(video, 'transcribe'));
  const [summarizeJob, setSummarizeJob] = useState<Job | null>(latestJob(video, 'summarize'));
  const [templateId, setTemplateId] = useState('');
  const [summaryReady, setSummaryReady] = useState(true);
  const [hasTranscript, setHasTranscript] = useState(Boolean(video.has_transcript));
  const [hasSummary, setHasSummary] = useState(Boolean(video.has_summary));
  const [busy, setBusy] = useState(false);
  const platform = platforms.find((item) => item.id === video.platform);
  const analyzeOpen = panel === 'transcribe' || panel === 'summarize';
  const transcribeRunning = isRunning(transcribeJob);
  const summarizeRunning = isRunning(summarizeJob);

  useEffect(() => {
    setTranscribeJob(latestJob(video, 'transcribe'));
    setSummarizeJob(latestJob(video, 'summarize'));
    setHasTranscript(Boolean(video.has_transcript));
    setHasSummary(Boolean(video.has_summary));
  }, [video.id, video.last_processed_at, video.has_transcript, video.has_summary]);

  useEffect(() => {
    const current = panel === 'summarize' ? summarizeJob : panel === 'transcribe' ? transcribeJob : null;
    if (!current?.id || (current.logs && current.logs.length)) return;
    api<Job>(`/api/jobs/${current.id}`).then((next) => {
      if (next.job_type === 'summarize') setSummarizeJob(next);
      else setTranscribeJob(next);
    }).catch(() => undefined);
  }, [panel, transcribeJob?.id, summarizeJob?.id]);

  useEffect(() => {
    const active = [transcribeJob, summarizeJob].filter((item) => isRunning(item)) as Job[];
    if (!active.length) return;
    const timer = window.setInterval(async () => {
      try {
        await Promise.all(active.map(async (item) => {
          const next = await api<Job>(`/api/jobs/${item.id}`);
          if (next.job_type === 'summarize') setSummarizeJob(next);
          else setTranscribeJob(next);
          if (['completed', 'failed', 'cancelled'].includes(next.status)) {
            const fresh = await api<VideoAsset>(`/api/videos/${video.id}`);
            setHasTranscript(Boolean(fresh.has_transcript || fresh.transcripts?.length));
            setHasSummary(Boolean(fresh.has_summary || fresh.summaries?.length));
          }
        }));
      } catch {
        window.clearInterval(timer);
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [transcribeJob?.id, transcribeJob?.status, summarizeJob?.id, summarizeJob?.status, video.id]);

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
    setBusy(true);
    changePanel(kind);
    try {
      const path = kind === 'transcribe' ? `/api/videos/${video.id}/transcribe` : `/api/videos/${video.id}/summary`;
      const created = await api<Job>(path, {
        method: 'POST',
        body: JSON.stringify({ video_id: video.id, template_id: templateId, reuse_transcript: true }),
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

  function takeJob(job: Job | null) {
    if (!job) return;
    if (job.job_type === 'summarize') setSummarizeJob(job);
    if (job.job_type === 'transcribe') setTranscribeJob(job);
  }

  if (loading) {
    return (
      <section className="result-card skeleton-card" aria-busy="true">
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
  // The action in the log panel also serves as an explicit re-run entry point.
  // Existing results remain available from the buttons on the left, so a
  // completed result must not leave the selected log panel empty.
  const transcribeStart = !transcribeRunning;
  const summaryStart = !summarizeRunning;
  const blockedHint = transcribeRunning && panel === 'summarize'
    ? '转录进行中，完成后可开始总结'
    : summarizeRunning && panel === 'transcribe'
      ? '总结进行中，完成后可开始转录'
      : panel === 'summarize' && !transcribeDone
        ? '还没有完成的转录，开始后会先转录再总结'
        : undefined;

  return (
    <section className={`result-card ${panel ? 'is-open' : ''} ${analyzeOpen ? 'is-summary' : ''} ${panel === 'download' ? 'is-download' : ''}`}>
      <div className="result-side">
        <div className="result-cover">
          {video.thumbnail_url ? <img src={video.thumbnail_url} alt="视频封面" /> : (
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
              resetKey={video.last_processed_at || video.id}
              initialJob={currentJob}
              hasTranscript={hasTranscript}
              hasSummary={hasSummary}
              showLaunch={false}
              onToast={onToast}
              onJob={takeJob}
              onTemplateId={setTemplateId}
              onSummaryReady={setSummaryReady}
              onResultState={({ transcript, summary }) => {
                setHasTranscript(transcript);
                setHasSummary(summary);
              }}
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
            <span className={`status-text ${video.parse_status}`}>{video.parse_status === 'parsed' ? '解析完成' : '等待解析器接入'}</span>
            <button type="button" className="text-button" onClick={() => { setTranscribeJob(null); setSummarizeJob(null); onReparse(); }}>重新解析</button>
          </div>
        </div>
        <h2>{video.title || '待获取视频标题'}</h2>
        <p className="meta-line">
          {video.author || '作者信息待获取'}
          <i />
          {formatDuration(video.duration_seconds)}
        </p>
        {video.source_url && (
          <a className="source-link" href={video.source_url} target="_blank" rel="noreferrer">
            {video.source_url}
          </a>
        )}
        <div className="result-actions">
          <button type="button" className={panel === 'download' ? 'primary-button' : 'outline-button'} onClick={() => changePanel(panel === 'download' ? null : 'download')}>
            视频下载
          </button>
          <button
            type="button"
            className={panel === 'transcribe' ? 'primary-button' : 'outline-button'}
            onClick={() => switchAnalyze('transcribe')}
          >
            {transcribeRunning ? '转录中…' : '转录'}
          </button>
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
              <SummaryProgress
                job={transcribeJob}
                startLabel="开始转录"
                startHint={blockedHint}
                showStart={transcribeStart}
                startDisabled={busy || transcribeRunning || summarizeRunning}
                onStart={() => start('transcribe')}
              />
            </div>
            <div className={`result-panel ${panel === 'summarize' ? 'is-active' : ''}`}>
              <SummaryProgress
                job={summarizeJob}
                startLabel="开始总结"
                startHint={blockedHint}
                showStart={summaryStart}
                startDisabled={busy || transcribeRunning || summarizeRunning}
                onStart={() => start('summarize')}
              />
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
