'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { api } from '../lib/api';
import type { Job, Summary, SummarySettings, Template, Transcript, VideoAsset } from '../lib/types';
import { cleanSummaryMarkdown } from '../lib/summary-text';
import { renderMarkdown } from '../lib/markdown';
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

function formatSize(bytes: number) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value >= 10 || index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`;
}

type FilesInfo = {
  folder: string | null;
  folder_openable: boolean;
  files: { kind: string; name: string; path: string; size: number }[];
};

export default function TranscriptStudio({
  videoId,
  meetingId,
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
  /** 传入会议 id 时面板会出现「重新转录」按钮，用已保存的录音重跑终稿转录。 */
  meetingId?: string;
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
  const [retrying, setRetrying] = useState(false);
  const [settings, setSettings] = useState<SummarySettings | null>(null);
  const [savingModel, setSavingModel] = useState(false);
  const [activeTemplateId, setActiveTemplateId] = useState(templateId);
  const [fileInfo, setFileInfo] = useState<FilesInfo | null>(null);
  const [filesLoading, setFilesLoading] = useState(false);
  const pollErrorShown = useRef(false);
  const [editingTranscript, setEditingTranscript] = useState(false);
  const [draftTurns, setDraftTurns] = useState<Transcript['segments']>([]);
  const [editingSummary, setEditingSummary] = useState(false);
  const [summaryDraft, setSummaryDraft] = useState('');
  const [selectedSummaryId, setSelectedSummaryId] = useState('');
  const [savingEdit, setSavingEdit] = useState(false);
  const [mobilePane, setMobilePane] = useState<'transcript' | 'summary'>('transcript');

  async function load() {
    const next = await api<VideoAsset>(`/api/videos/${videoId}`);
    setVideo(next);
    // 从列表/历史直接点进面板时，终稿转录可能正在跑：发现活动的转录任务就接管，
    // 左侧立即出现转录状态与进度，完成后由下方轮询自动刷新出文字。
    const active = (next.jobs || []).find(
      (item) => item.job_type === 'transcribe' && !['completed', 'failed', 'cancelled'].includes(item.status),
    );
    setJob((current) => {
      if (current && !['completed', 'failed', 'cancelled'].includes(current.status)) return current;
      return active || current || null;
    });
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
        pollErrorShown.current = false;
        setJob(next);
        onJobChange?.(next);
        if (['completed', 'failed', 'cancelled'].includes(next.status)) {
          await load();
        }
      } catch (err) {
        // 临时断网不应永久停止轮询；网络恢复后继续接管任务状态。
        if (!pollErrorShown.current) {
          pollErrorShown.current = true;
          onToast(`任务状态暂时无法刷新：${(err as Error).message}`, 'error');
        }
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [job?.id, job?.status]);

  const transcript: Transcript | undefined = video?.transcripts?.[0];
  const summary: Summary | undefined = video?.summaries?.find((item) => item.id === selectedSummaryId) || video?.summaries?.[0];
  const summaryText = cleanSummaryMarkdown(summary?.content_markdown);
  const summaryHtml = useMemo(() => renderMarkdown(summaryText), [summaryText]);
  // 导出文件名用视频标题，去掉文件系统不允许的字符。
  const fileBase = (video?.title || '总结').replace(/[\\/:*?"<>|]/g, '_').trim().slice(0, 80) || '总结';
  const exportRef = useRef<HTMLDetailsElement>(null);
  const turns: Transcript['segments'] = useMemo(() => {
    const segments = transcript?.segments || [];
    if (segments.length) return segments.filter((item) => (item.text || '').trim());
    if (!transcript?.text) return [];
    return transcript.text.split('\n').filter(Boolean).map((text) => ({ text }));
  }, [transcript]);

  useEffect(() => {
    const summaries = video?.summaries || [];
    if (!summaries.length) {
      setSelectedSummaryId('');
      return;
    }
    setSelectedSummaryId((current) => summaries.some((item) => item.id === current) ? current : summaries[0].id);
  }, [video?.summaries]);

  function beginTranscriptEdit() {
    setDraftTurns(turns.map((item) => ({ ...item })));
    setEditingTranscript(true);
  }

  function renameDraftSpeaker(oldName: string | null | undefined, nextName: string) {
    setDraftTurns((current) => current.map((item) => (
      (item.speaker || '') === (oldName || '') ? { ...item, speaker: nextName } : item
    )));
  }

  async function saveTranscriptEdit() {
    if (!transcript || savingEdit) return;
    const segments = draftTurns.map((item) => ({ ...item, text: (item.text || '').trim() }));
    const text = segments.map((item) => item.text || '').filter(Boolean).join('\n');
    if (!text) {
      onToast('转录内容不能为空', 'error');
      return;
    }
    setSavingEdit(true);
    try {
      await api(`/api/transcripts/${transcript.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ text, segments }),
      });
      await load();
      setEditingTranscript(false);
      onToast('校对结果已保存为新版本');
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setSavingEdit(false);
    }
  }

  function beginSummaryEdit() {
    setSummaryDraft(summaryText);
    setEditingSummary(true);
  }

  async function saveSummaryEdit() {
    if (!summary || !summaryDraft.trim() || savingEdit) return;
    setSavingEdit(true);
    try {
      await api(`/api/summaries/${summary.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ content_markdown: summaryDraft }),
      });
      await load();
      setEditingSummary(false);
      onToast('纪要修改已保存为新版本');
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setSavingEdit(false);
    }
  }

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

  function closeExportMenu() {
    exportRef.current?.removeAttribute('open');
  }

  // 总结的两种导出：下载 .md，或把渲染后的结果包成独立页面另开标签预览。
  // Markdown 源文件交给外部编辑器，预览走的是和面板同一份 renderMarkdown 输出。
  function exportSummary(kind: 'download' | 'preview') {
    if (!summaryText) return;
    if (kind === 'download') {
      const blob = new Blob([summaryText], { type: 'text/markdown;charset=utf-8' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `${fileBase}.md`;
      link.click();
      // 不能在 click 后立刻吊销：浏览器把下载排队成异步任务时，吊销 blob URL
      // 会直接把下载取消掉。延迟回收，既不泄漏也不打断下载。
      window.setTimeout(() => URL.revokeObjectURL(link.href), 10_000);
      onToast('已导出 Markdown 文档');
      return;
    }
    const document_ = [
      '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
      `<title>${fileBase}</title>`,
      '<meta name="viewport" content="width=device-width,initial-scale=1">',
      '<style>',
      'body{margin:0;background:#f6f8fb;font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#1f2933;}',
      'article{max-width:760px;margin:32px auto;padding:36px 40px;background:#fff;border-radius:14px;box-shadow:0 8px 28px rgba(22,32,48,.08);line-height:1.75;font-size:15px}',
      'h1,h2,h3,h4{line-height:1.4;margin:24px 0 10px}h1{font-size:22px}h2{font-size:18px}h3{font-size:16px}',
      'ul,ol{padding-left:24px;margin:0 0 12px}li{margin:5px 0}',
      'strong{font-weight:650}code{background:#eef2f6;border-radius:5px;padding:1px 6px;font-size:13px}',
      'blockquote{margin:0 0 12px;padding:2px 0 2px 14px;border-left:3px solid #c9d6e5;color:#5c6b80}',
      'hr{border:0;border-top:1px solid #e3e7ed;margin:20px 0}a{color:#1d68bb}',
      '</style></head><body><article>', summaryHtml, '</article></body></html>',
    ].join('');
    const blob = new Blob([document_], { type: 'text/html;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    window.open(url, '_blank', 'noopener');
    // 给新标签留出加载时间再回收，避免某些浏览器直接取消打开。
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
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

  // 查看任务在磁盘上的落盘文件（录音/媒体）与所在目录；转录与纪要在数据库里。
  async function loadFiles() {
    setFilesLoading(true);
    try {
      setFileInfo(await api<FilesInfo>(`/api/videos/${videoId}/files`));
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setFilesLoading(false);
    }
  }

  async function openFolder() {
    try {
      await api(`/api/videos/${videoId}/files/open`, { method: 'POST' });
    } catch (err) {
      onToast((err as Error).message, 'error');
    }
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

  // 会议场景专属：录音文件已落盘（meeting.audio_path），失败/中断后不必重录，
  // 直接重跑终稿转录。job 挂上去后由上方现有轮询接管，完成后自动 load() 刷新转录。
  async function retryTranscribe() {
    if (retrying || (!meetingId && !job?.id)) return;
    setRetrying(true);
    try {
      if (meetingId) {
        const payload = await api<{ job?: Job }>(`/api/v1/meetings/${meetingId}/retry-transcribe`, { method: 'POST' });
        if (payload.job) {
          setJob(payload.job);
          onJobChange?.(payload.job);
        }
      } else if (job?.id) {
        const created = await api<Job>(`/api/jobs/${job.id}/retry`, { method: 'POST' });
        setJob(created);
        onJobChange?.(created);
      }
      onToast('已重新开始终稿转录');
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setRetrying(false);
    }
  }

  const generating = Boolean(job && !['completed', 'failed', 'cancelled'].includes(job.status));
  const transcribeRunning = generating && job?.job_type === 'transcribe';
  const failedTranscribe = job?.status === 'failed' && job?.job_type === 'transcribe';
  const failed = job?.status === 'failed';
  const progress = Math.max(0, Math.min(100, Math.round((job?.progress || 0) * 100)));
  const dialog = (
    <div className="studio-mask" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <div className="studio-card" role="dialog" aria-modal="true" aria-label="转录与总结结果">
        <header className="studio-titlebar">
          <strong>{video?.title || '转录结果'}</strong>
          <div className="studio-titlebar-actions">
            {/* 查看该任务在磁盘上的文件（录音/媒体）与所在目录 */}
            <button type="button" className="studio-tool" onClick={loadFiles} disabled={filesLoading}>
              {filesLoading ? '读取中…' : '文件'}
            </button>
            {/* 导出：转录给复制（两种粒度），总结给文档格式（当前支持 Markdown）。 */}
            <details className="export-menu" ref={exportRef}>
              <summary aria-label="导出">导出</summary>
              <div className="copy-menu is-right" role="menu">
                <p className="export-group">转录</p>
                <button type="button" disabled={!transcript} onClick={() => { closeExportMenu(); copyTranscript(false); }}>仅复制内容</button>
                <button type="button" disabled={!transcript} onClick={() => { closeExportMenu(); copyTranscript(true); }}>复制内容与说话人</button>
                <p className="export-group">总结</p>
                <button type="button" disabled={!summaryText} onClick={() => { closeExportMenu(); exportSummary('download'); }}>Markdown 文档（.md）</button>
                <button type="button" disabled={!summaryText} onClick={() => { closeExportMenu(); exportSummary('preview'); }}>预览文档</button>
              </div>
            </details>
            <button type="button" className="studio-close" onClick={onClose} aria-label="关闭">×</button>
          </div>
        </header>
        <div className="studio-mobile-tabs" role="tablist" aria-label="结果类型">
          <button type="button" className={mobilePane === 'transcript' ? 'active' : ''} onClick={() => setMobilePane('transcript')}>转录</button>
          <button type="button" className={mobilePane === 'summary' ? 'active' : ''} onClick={() => setMobilePane('summary')}>纪要</button>
        </div>
        <div className="studio-body">
          <section className={`studio-pane${mobilePane !== 'transcript' ? ' mobile-pane-hidden' : ''}`}>
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
              {transcript && !editingTranscript ? (
                <button type="button" className="studio-tool" onClick={beginTranscriptEdit}>校对转录</button>
              ) : editingTranscript ? (
                <>
                  <button type="button" className="studio-tool active" disabled={savingEdit} onClick={saveTranscriptEdit}>
                    {savingEdit ? '保存中…' : '保存校对版'}
                  </button>
                  <button type="button" className="studio-tool" disabled={savingEdit} onClick={() => setEditingTranscript(false)}>取消</button>
                </>
              ) : null}
              {/* 会议录音已落盘，转录失败/中断后可在这里直接重跑，不必回工作台重录。 */}
              {meetingId && (
                <button
                  type="button"
                  className="studio-tool"
                  data-tip="用已保存的录音重跑终稿转录"
                  onClick={retryTranscribe}
                  disabled={retrying || generating}
                >
                  <Icon d="M12 5V1L7 6l5 5V7a5 5 0 11-5 5H5a7 7 0 107-7z" />
                  {retrying ? '正在启动…' : transcribeRunning ? '转录中…' : '重新转录'}
                </button>
              )}
            </div>
          <div className="studio-turns">
            {!transcript && transcribeRunning ? (
              <div className="studio-empty studio-transcribing">
                <span className="studio-spinner" />
                <h3>正在转录录音</h3>
                <p>{job?.progress_detail?.message || '模型正在听录音，完成后文字会出现在这里。'}</p>
                <div className="studio-progress"><i style={{ width: `${Math.max(progress, 8)}%` }} /></div>
                <small>{progress}%</small>
              </div>
            ) : !transcript ? (
              <p className="studio-empty-text">还没有转录结果。</p>
            ) : null}
            {(editingTranscript ? draftTurns : turns).map((item, index) => (
              <article className="studio-turn" key={`${item.text}-${index}`}>
                <time>[{clock(segmentStart(item))}]</time>
                <div>
                  {editingTranscript ? (
                    <>
                      <input
                        className="studio-speaker-input"
                        value={item.speaker || ''}
                        aria-label={`第 ${index + 1} 段说话人`}
                        placeholder="说话人"
                        onChange={(event) => renameDraftSpeaker(item.speaker, event.target.value)}
                      />
                      <textarea
                        className="studio-transcript-editor"
                        value={item.text || ''}
                        aria-label={`第 ${index + 1} 段转录内容`}
                        onChange={(event) => setDraftTurns((current) => current.map((turn, turnIndex) => (
                          turnIndex === index ? { ...turn, text: event.target.value } : turn
                        )))}
                      />
                    </>
                  ) : (
                    <>
                      {item.speaker && <em>{item.speaker}</em>}
                      <p>{item.text}</p>
                    </>
                  )}
                </div>
              </article>
            ))}
          </div>
          </section>
          <section className={`studio-pane${mobilePane !== 'summary' ? ' mobile-pane-hidden' : ''}`}>
            <div className="studio-pane-toolbar studio-summary-toolbar">
              {(video?.summaries?.length || 0) > 1 && (
                <select
                  aria-label="纪要版本"
                  value={summary?.id || ''}
                  onChange={(event) => setSelectedSummaryId(event.target.value)}
                >
                  {(video?.summaries || []).map((item, index) => (
                    <option key={item.id} value={item.id}>
                      版本 {(video?.summaries?.length || 0) - index} · {new Date(item.created_at).toLocaleString('zh-CN')}
                    </option>
                  ))}
                </select>
              )}
              {summaryText && !editingSummary ? (
                <button type="button" className="studio-tool" onClick={beginSummaryEdit}>编辑纪要</button>
              ) : editingSummary ? (
                <>
                  <button type="button" className="studio-tool active" disabled={savingEdit} onClick={saveSummaryEdit}>
                    {savingEdit ? '保存中…' : '保存新版本'}
                  </button>
                  <button type="button" className="studio-tool" disabled={savingEdit} onClick={() => setEditingSummary(false)}>取消</button>
                </>
              ) : null}
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
                  ...templates.map((item) => ({ value: item.id, label: item.name, hint: item.is_default ? '默认' : undefined })),
                ]}
                onChange={changeTemplate}
              />
            </div>
            {generating ? (
              <div className="studio-empty studio-processing">
                <span className="studio-spinner" />
                <h3>{transcribeRunning ? '正在转录录音' : '正在生成摘要'}</h3>
                <p>{job?.progress_detail?.message || (transcribeRunning ? '模型正在听录音，请稍候。' : '模型正在读取转录内容，请稍候。')}</p>
                <div className="studio-progress"><i style={{ width: `${Math.max(progress, 8)}%` }} /></div>
                <small>{progress}%</small>
              </div>
            ) : failed ? (
              <div className="studio-empty studio-failed">
                <span className="studio-empty-icon">!</span>
                <h3>{failedTranscribe ? '重新转录失败' : '摘要生成失败'}</h3>
                <p>{job?.error_message || (failedTranscribe ? '可以再次尝试重新转录。' : '生成摘要时发生错误，请重试。')}</p>
                <button type="button" className="primary-button" onClick={failedTranscribe ? retryTranscribe : generate}>
                  {failedTranscribe ? '重新转录' : '重新生成摘要'}
                </button>
              </div>
            ) : summaryText ? (
            editingSummary ? (
              <textarea
                className="studio-summary-editor"
                value={summaryDraft}
                aria-label="编辑会议纪要 Markdown"
                onChange={(event) => setSummaryDraft(event.target.value)}
              />
            ) : (
              <article
                className="studio-summary markdown-body rich-text"
                // renderMarkdown 输出前已整体做 HTML 转义，这里不会有注入面。
                dangerouslySetInnerHTML={{ __html: summaryHtml }}
              />
            )
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
      {fileInfo && (
        <div
          className="confirm-mask"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setFileInfo(null);
          }}
        >
          <div className="confirm-box files-box">
            <h3>任务文件</h3>
            {fileInfo.files.length ? (
              <>
                <p className="files-folder">{fileInfo.folder}</p>
                <ul className="files-list">
                  {fileInfo.files.map((item) => (
                    <li key={item.path}>
                      <em>{item.kind}</em>
                      <strong>{item.name}</strong>
                      <small>{formatSize(item.size)}</small>
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <p className="files-folder">该任务还没有落盘文件。</p>
            )}
            <p className="files-note">已有转录/纪要时，打开文件夹即可看到「转录全文.txt」「总结纪要.md」；本面板内也可直接复制或通过「导出」生成文档。</p>
            <div className="detail-actions">
              {fileInfo.folder_openable && (
                <button type="button" className="primary-button" onClick={openFolder}>
                  打开文件夹
                </button>
              )}
              <button type="button" className="outline-button" onClick={() => setFileInfo(null)}>
                关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
  if (typeof document === 'undefined') return dialog;
  return createPortal(dialog, document.body);
}
