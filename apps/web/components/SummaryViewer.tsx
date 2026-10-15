'use client';

import { useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api';
import type { Summary, Template } from '../lib/types';
import { cleanSummaryMarkdown } from '../lib/summary-text';
import { renderMarkdown } from '../lib/markdown';

export default function SummaryViewer({
  summaries,
  templates,
  onRetry,
  onToast,
}: {
  summaries: Summary[];
  templates: Template[];
  onRetry: () => void;
  onToast: (message: string) => void;
}) {
  const [currentId, setCurrentId] = useState(summaries[0]?.id || '');
  const [items, setItems] = useState(summaries);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const current = items.find((item) => item.id === currentId) || items[0];
  const templateName = templates.find((item) => item.id === current?.template_id)?.name || '未指定模板';
  const content = cleanSummaryMarkdown(current?.content_markdown);
  // 面板与查看器共用同一套渲染：转义后转换，模型输出不会变成标签。
  const contentHtml = useMemo(() => renderMarkdown(content), [content]);

  useEffect(() => {
    setItems(summaries);
    setCurrentId((id) => summaries.some((item) => item.id === id) ? id : summaries[0]?.id || '');
  }, [summaries]);

  if (!current) {
    return (
      <div className="summary-placeholder">
        <span>✦</span>
        <h3>还没有总结结果</h3>
        <p>选择“重新总结”后，结果会按版本保存在这里。</p>
        <button type="button" className="outline-button" onClick={onRetry}>重新总结</button>
      </div>
    );
  }

  async function copy() {
    await navigator.clipboard.writeText(content);
    onToast('总结已复制');
  }

  function download(ext: 'md' | 'txt') {
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `summary.${ext}`;
    link.click();
    // click 后立刻吊销会把排队中的下载取消掉，延迟回收。
    window.setTimeout(() => URL.revokeObjectURL(link.href), 10_000);
    onToast(`已下载 ${ext.toUpperCase()}`);
  }

  async function saveEdit() {
    if (!current || !draft.trim() || saving) return;
    setSaving(true);
    try {
      const created = await api<Summary>(`/api/summaries/${current.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ content_markdown: draft }),
      });
      setItems((existing) => [created, ...existing]);
      setCurrentId(created.id);
      setEditing(false);
      onToast('纪要修改已保存为新版本');
    } catch (err) {
      onToast((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="viewer">
      <div className="viewer-toolbar">
        <select value={current.id} onChange={(event) => setCurrentId(event.target.value)}>
          {items.map((item, index) => (
            <option key={item.id} value={item.id}>版本 {items.length - index} · {new Date(item.created_at).toLocaleString('zh-CN')}</option>
          ))}
        </select>
        <button type="button" onClick={copy}>复制总结</button>
        <button type="button" onClick={() => download('md')}>下载 Markdown</button>
        <button type="button" onClick={() => download('txt')}>下载 TXT</button>
        {!editing ? (
          <button type="button" onClick={() => { setDraft(content); setEditing(true); }}>编辑纪要</button>
        ) : (
          <>
            <button type="button" disabled={saving} onClick={saveEdit}>{saving ? '保存中…' : '保存新版本'}</button>
            <button type="button" disabled={saving} onClick={() => setEditing(false)}>取消</button>
          </>
        )}
        <button type="button" onClick={onRetry}>重新总结</button>
      </div>
      <p className="viewer-meta">模板 {templateName} · {current.provider} / {current.model} · {new Date(current.created_at).toLocaleString('zh-CN')}</p>
      {editing ? (
        <textarea className="viewer-scroll studio-summary-editor" value={draft} onChange={(event) => setDraft(event.target.value)} />
      ) : (
        <article
          className="viewer-scroll markdown-body rich-text"
          dangerouslySetInnerHTML={{ __html: contentHtml || '<p>这次没有可用总结，模型内部过程已过滤，请重新总结。</p>' }}
        />
      )}
    </div>
  );
}
