'use client';

import { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { api } from '../lib/api';
import type { Job, Summary, SummarySettings, Template, Transcript, VideoAsset } from '../lib/types';
import { cleanSummaryMarkdown } from '../lib/summary-text';
import StudioSelect from './StudioSelect';

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
  templates = [],
  summaryReady,
  activeJob,
  onClose,
  onToast,
  onJobChange,
  onTemplateChange,
}: {
  videoId: string;
  templateId: string;
  templateName?: string;
  templates?: Template[];
  summaryReady: boolean;
  activeJob?: Job | null;
  onClose: () => void;
  onToast: (message: string, kind?: 'success' | 'error') => void;
  onJobChange?: (job: Job) => void;
  onTemplateChange?: (id: string) => void;
}) {
  const [video, setVideo] = useState<VideoAsset | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [settings, setSettings] = useState<SummarySettings | null>(null);
  const [savingModel, setSavingModel] = useState(false);
  const [activeTemplateId, setActiveTemplateId] = useState(templateId);

  async function load() {
    const next = await api<VideoAsset>(`/api/videos/${videoId}`);
    setVideo(next);
    return next;
  }

  useEffect(() => {
    load().catch((err) => onToast((err as Error).message, 'error'));
  }, [videoId]);

  // The summary model lives in the global settings; read it so the picker can
  // show what is actually configured instead of sending the user to /settings.
  useEffect(() => {
    api<SummarySettings>('/api/settings/transcription')
      .then(setSettings)
      .catch(() => setSettings(null));
  }, []);

  useEffect(() => {
    setActiveTemplateId(templateId);
  }, [templateId]);

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

  // The model picker mirrors the provider list from the settings page, but the
  // choice is written straight through so a summary can be re-run without
  // leaving the dialog. `summary_model` is the model name for local and remote.
  //
  // 内置本地模型由 MOSS 用 llama-cli 加载 summary_model_path 指向的 GGUF 完成，
  // 所以可用性跟着 summary_model_found（该文件是否存在）走；远程 provider 则需要
  // 配了 api_base_url 才算可用，否则必然失败。
  // 设置还在请求中时 settings 为 null。这段时间不能把「还没读到」当成「未配置」，
  // 否则面板一打开会先闪一下「未配置总结模型」，看起来像模型没配好。父组件通常先
  // 挂载、先拿到配置，所以加载期间先沿用它的结论，措辞也改成中性的「正在读取」。
  const settingsLoading = settings === null;
  const provider = settings?.default_provider || 'local';
  const modelName = settings?.summary_model || '';
  const hasRemoteBase = Boolean(settings?.api_base_url);
  const localUsable = Boolean(settings?.summary_model_found);
  const modelReady = settingsLoading ? summaryReady : (provider === 'local' ? localUsable : hasRemoteBase);
  const modelUnavailableTip = settingsLoading
    ? '正在读取模型配置…'
    : provider === 'local'
      ? `本地总结不可用：找不到 ${settings?.summary_model_path || '总结模型文件'}`
      : '远程总结不可用：请先在设置里填写 API Base URL';
  const modelOptions = [
    {
      value: 'local',
      label: modelName ? `内置本地模型 · ${modelName}` : '内置本地模型',
      usable: localUsable,
      hint: settingsLoading ? '正在读取配置…' : localUsable ? undefined : '缺少 GGUF 模型文件',
    },
    {
      value: 'ollama',
      label: modelName ? `Ollama · ${modelName}` : 'Ollama',
      usable: hasRemoteBase,
      hint: settingsLoading ? '正在读取配置…' : hasRemoteBase ? undefined : '未配置接口地址',
    },
    {
      value: 'openai',
      label: modelName ? `OpenAI 兼容 API · ${modelName}` : 'OpenAI 兼容 API',
      usable: hasRemoteBase,
      hint: settingsLoading ? '正在读取配置…' : hasRemoteBase ? undefined : '未配置接口地址',
    },
  ];

  async function changeModel(nextProvider: string) {
    if (!settings || nextProvider === provider || savingModel) return;
    const previous = settings;
    setSavingModel(true);
    setSettings({ ...settings, default_provider: nextProvider });
    try {
      const updated = await api<SummarySettings>('/api/settings/transcription', {
        method: 'PUT',
        body: JSON.stringify({ default_provider: nextProvider }),
      });
      setSettings(updated);
      const label = modelOptions.find((item) => item.value === nextProvider)?.label || nextProvider;
      onToast(`总结模型已切换为 ${label}（全局设置，下次生成生效）`);
    } catch (err) {
      setSettings(previous);
      onToast((err as Error).message, 'error');
    } finally {
      setSavingModel(false);
    }
  }

  function changeTemplate(id: string) {
    setActiveTemplateId(id);
    onTemplateChange?.(id);
  }

  async function generate() {
    if (!modelReady) {
      onToast('当前总结模型不可用，请在上方「AI 模型」里切换，或到设置页完成配置', 'error');
      return;
    }
    setBusy(true);
    try {
      const created = await api<Job>(`/api/videos/${videoId}/summaries`, {
        method: 'POST',
        body: JSON.stringify({ template_id: activeTemplateId }),
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
                className={modelReady ? 'studio-tool active' : 'studio-tool muted-button'}
                data-tip={settingsLoading || modelReady ? undefined : '未配置总结模型'}
                onClick={generate}
                disabled={busy || generating || !transcript}
              >
                <Icon d="M12 2l1.6 4.4L18 8l-4.4 1.6L12 14l-1.6-4.4L6 8l4.4-1.6L12 2zm7 9l.9 2.6L23 15l-3.1.9L19 19l-.9-3.1L15 15l3.1-.9L19 11zM5 14l.8 2.2L8 17l-2.2.8L5 20l-.8-2.2L2 17l2.2-.8L5 14z" />
                {generating ? '生成中…' : summaryText ? '重新生成摘要' : '生成摘要'}
              </button>
              <StudioSelect
                icon="M12.9 15h-1.8L9.6 11.4 8 15H6.2l2.8-7h1.6l2.3 7zM4 4h16v2H4V4zm2 14h12v2H6v-2z"
                value="zh-CN"
                ariaLabel="摘要语言"
                title="摘要语言"
                options={[{ value: 'zh-CN', label: '简体中文' }]}
                onChange={() => undefined}
              />
              <StudioSelect
                icon="M12 2a4 4 0 014 4v1h1a3 3 0 013 3v2h-2v5a3 3 0 01-3 3H9a3 3 0 01-3-3v-5H4V10a3 3 0 013-3h1V6a4 4 0 014-4zm-1 14v3h2v-3h-2zm0-8v6h2V8h-2z"
                value={provider}
                ariaLabel="总结模型"
                title={modelReady ? '选择总结模型' : modelUnavailableTip}
                muted={!settingsLoading && !modelReady}
                disabled={!settings || savingModel}
                placeholder="总结模型"
                maxWidth={190}
                options={modelOptions.map((item) => ({
                  value: item.value,
                  label: item.label,
                  hint: item.hint,
                  disabled: !item.usable,
                }))}
                onChange={changeModel}
              />
              <StudioSelect
                icon="M6 2h9l5 5v13a2 2 0 01-2 2H6a2 2 0 01-2-2V4a2 2 0 012-2zm8 1.5V8h4.5L14 3.5zM8 12h8v2H8v-2zm0 4h8v2H8v-2z"
                value={activeTemplateId}
                ariaLabel="总结模板"
                title={templateName || '选择总结模板'}
                disabled={!templates.length}
                placeholder={templates.length ? '选择模板' : '暂无可用模板'}
                maxWidth={190}
                options={[
                  ...(activeTemplateId && !templates.some((item) => item.id === activeTemplateId)
                    ? [{ value: activeTemplateId, label: templateName || '当前模板', hint: '不在可用列表' }]
                    : []),
                  ...templates.map((item) => ({ value: item.id, label: item.name })),
                ]}
                onChange={changeTemplate}
              />
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
                className={modelReady ? 'primary-button' : 'muted-button'}
                data-tip={modelReady ? undefined : '未配置总结模型'}
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
                className={modelReady ? 'primary-button' : 'muted-button'}
                data-tip={modelReady ? undefined : '未配置总结模型'}
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
