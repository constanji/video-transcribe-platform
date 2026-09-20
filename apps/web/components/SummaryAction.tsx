'use client';

import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import type { Job, Template, VideoAsset } from '../lib/types';
import TemplateEditor from './TemplateEditor';
import TemplatePicker from './TemplatePicker';
import TranscriptStudio from './TranscriptStudio';

function hasUsableTranscript(item: VideoAsset) {
  return Boolean(item.transcripts?.some((value) => (value.text || '').trim()));
}

function hasUsableSummary(item: VideoAsset) {
  return Boolean(item.summaries?.some((value) => (value.content_markdown || '').trim()));
}

export default function SummaryAction({
  videoId,
  resetKey,
  initialJob = null,
  hasTranscript = false,
  hasSummary = false,
  showLaunch = true,
  onToast,
  onJob,
  onTemplateId,
  onSummaryReady,
  onResultState,
}: {
  videoId: string;
  resetKey?: string;
  initialJob?: Job | null;
  hasTranscript?: boolean;
  hasSummary?: boolean;
  showLaunch?: boolean;
  onToast: (message: string, kind?: 'success' | 'error') => void;
  onJob?: (job: Job | null) => void;
  onTemplateId?: (id: string) => void;
  onSummaryReady?: (ready: boolean) => void;
  onResultState?: (state: { transcript: boolean; summary: boolean }) => void;
}) {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [settings, setSettings] = useState<any>({});
  const [templateId, setTemplateId] = useState('');
  const [job, setJob] = useState<Job | null>(initialJob);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [studio, setStudio] = useState(false);
  const [transcriptReady, setTranscriptReady] = useState(hasTranscript);
  const [summaryDone, setSummaryDone] = useState(hasSummary);
  const [moss, setMoss] = useState<{ loaded?: boolean; loading?: boolean; reachable?: boolean }>({});

  const summaryReady = settings.default_provider === 'local'
    ? Boolean(settings.summary_model_found)
    : Boolean(settings.api_base_url || settings.default_provider);

  useEffect(() => {
    onSummaryReady?.(summaryReady);
  }, [summaryReady, onSummaryReady]);

  useEffect(() => {
    onResultState?.({ transcript: transcriptReady, summary: summaryDone });
  }, [summaryDone, transcriptReady, onResultState]);

  useEffect(() => {
    api<Template[]>('/api/templates?listed=true').then((items) => {
      setTemplates(items);
      const preferred = items.find((item) => item.name === '通用视频总结') || items.find((item) => item.name === '新闻热点') || items[0];
      const nextId = preferred?.id || '';
      setTemplateId(nextId);
      onTemplateId?.(nextId);
    }).catch(() => setTemplates([]));
    api<any>('/api/settings/transcription').then((value) => {
      setSettings(value);
    }).catch(() => setSettings({}));
    api<any>('/api/system/health').then((health) => {
      setMoss({
        reachable: Boolean(health.moss_reachable),
        loaded: Boolean(health.moss_model_loaded),
        loading: Boolean(health.moss_model_loading),
      });
    }).catch(() => setMoss({ reachable: false }));
    if (videoId) {
      api<VideoAsset>(`/api/videos/${videoId}`).then((item) => {
        setTranscriptReady(hasUsableTranscript(item));
        setSummaryDone(hasUsableSummary(item));
      }).catch(() => undefined);
    }
  }, [videoId]);

  useEffect(() => {
    setJob(initialJob || null);
    setTranscriptReady(hasTranscript);
    setSummaryDone(hasSummary || (initialJob?.job_type === 'summarize' && initialJob.status === 'completed'));
  }, [videoId, resetKey, initialJob?.id, hasTranscript, hasSummary]);

  useEffect(() => {
    onJob?.(job);
  }, [job, onJob]);

  useEffect(() => {
    if (!job?.id || ['completed', 'failed', 'cancelled'].includes(job.status)) return;
    const timer = window.setInterval(async () => {
      try {
        const next = await api<Job>(`/api/jobs/${job.id}`);
        setJob(next);
        if (['completed', 'failed'].includes(next.status)) {
          const video = await api<VideoAsset>(`/api/videos/${videoId}`);
          setTranscriptReady(hasUsableTranscript(video));
          setSummaryDone(Boolean(hasUsableSummary(video) || (next.job_type === 'summarize' && next.status === 'completed' && (video.summaries || []).some((value) => (value.content_markdown || '').trim()))));
        }
      } catch {
        window.clearInterval(timer);
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [job?.id, job?.status, videoId]);

  useEffect(() => {
    if (moss.loaded) return;
    const timer = window.setInterval(async () => {
      try {
        const health = await api<any>('/api/system/health');
        setMoss({
          reachable: Boolean(health.moss_reachable),
          loaded: Boolean(health.moss_model_loaded),
          loading: Boolean(health.moss_model_loading),
        });
      } catch {
        setMoss({ reachable: false });
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [moss.loaded]);

  const running = Boolean(job && !['completed', 'failed', 'cancelled'].includes(job.status));
  const transcribeDone = Boolean(transcriptReady || (job?.job_type === 'transcribe' && job.status === 'completed'));

  async function start(kind: 'transcribe' | 'summary') {
    if (kind === 'summary' && !summaryReady) {
      window.location.href = '/settings?tab=transcription';
      return;
    }
    if (kind === 'summary' && !templateId) return;
    setBusy(true);
    try {
      const path = kind === 'transcribe' ? `/api/videos/${videoId}/transcribe` : `/api/videos/${videoId}/summary`;
      const created = await api<Job>(path, {
        method: 'POST',
        body: JSON.stringify({ video_id: videoId, template_id: templateId, reuse_transcript: true }),
      });
      setJob(created);
      onToast(kind === 'transcribe' ? '转录已开始，进度见右侧日志' : '正在按模板转录并总结');
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setBusy(false);
    }
  }

  function changeTemplate(id: string) {
    setTemplateId(id);
    onTemplateId?.(id);
  }

  return (
    <div className="summary-start">
      <div className="field-stack">
        <span>总结模板</span>
        <TemplatePicker
          compact
          templates={templates}
          value={templateId}
          onChange={changeTemplate}
          onAdd={() => setEditing(true)}
        />
      </div>
      {showLaunch && (
        <div className="summary-actions">
          <button type="button" className="outline-button" onClick={() => start('transcribe')} disabled={busy || running}>
            {busy ? '创建中…' : running && job?.job_type === 'transcribe' ? '转录中…' : '转录'}
          </button>
          <button
            type="button"
            className={summaryReady ? 'primary-button' : 'muted-button'}
            data-tip={summaryReady ? undefined : '未配置总结模型'}
            onClick={() => start('summary')}
            disabled={busy || running}
          >
            {busy ? '创建中…' : running && job?.job_type === 'summarize' ? '总结中…' : '总结'}
          </button>
        </div>
      )}
      <div className="summary-result-actions">
        <button
          type="button"
          className={transcribeDone ? 'result-ready-button' : 'muted-button'}
          onClick={() => transcribeDone && setStudio(true)}
          disabled={!transcribeDone}
        >
          查看转录结果
        </button>
        <button
          type="button"
          className={summaryDone ? 'result-ready-button' : 'muted-button'}
          onClick={() => summaryDone && setStudio(true)}
          disabled={!summaryDone}
        >
          查看总结结果
        </button>
      </div>
      {moss.reachable === false && (
        <p className="inline-warning">转录服务未连接，请先启动本机 MOSS。</p>
      )}
      {moss.reachable !== false && moss.loading && !moss.loaded && (
        <p className="inline-warning">模型加载中，就绪后再开始可避免第一次等待。</p>
      )}
      {!summaryReady && showLaunch && <p className="inline-warning">总结模型未配置，点击灰色按钮可去设置。</p>}
      <TemplateEditor
        open={editing}
        title="增加模板"
        onClose={() => setEditing(false)}
        onSaved={(created) => {
          if (created.show_on_home === false) return;
          setTemplates((current) => [created, ...current.filter((item) => item.id !== created.id)]);
          changeTemplate(created.id);
        }}
        onToast={onToast}
      />
      {studio && (
        <TranscriptStudio
          videoId={videoId}
          templateId={templateId}
          templateName={templates.find((item) => item.id === templateId)?.name}
          summaryReady={summaryReady}
          activeJob={job}
          onJobChange={setJob}
          onClose={() => setStudio(false)}
          onToast={onToast}
        />
      )}
    </div>
  );
}
