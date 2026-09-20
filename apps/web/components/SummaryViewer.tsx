'use client';

import { useState } from 'react';
import type { Summary, Template } from '../lib/types';
import { cleanSummaryMarkdown } from '../lib/summary-text';

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
  const current = summaries.find((item) => item.id === currentId) || summaries[0];
  const templateName = templates.find((item) => item.id === current?.template_id)?.name || '未指定模板';
  const content = cleanSummaryMarkdown(current?.content_markdown);

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
    onToast(`已下载 ${ext.toUpperCase()}`);
  }

  return (
    <div className="viewer">
      <div className="viewer-toolbar">
        <select value={current.id} onChange={(event) => setCurrentId(event.target.value)}>
          {summaries.map((item, index) => (
            <option key={item.id} value={item.id}>版本 {summaries.length - index} · {new Date(item.created_at).toLocaleString('zh-CN')}</option>
          ))}
        </select>
        <button type="button" onClick={copy}>复制总结</button>
        <button type="button" onClick={() => download('md')}>下载 Markdown</button>
        <button type="button" onClick={() => download('txt')}>下载 TXT</button>
        <button type="button" onClick={onRetry}>重新总结</button>
      </div>
      <p className="viewer-meta">模板 {templateName} · {current.provider} / {current.model} · {new Date(current.created_at).toLocaleString('zh-CN')}</p>
      <article className="viewer-scroll markdown-body">
        {content || '这次没有可用总结，模型内部过程已过滤，请重新总结。'}
      </article>
    </div>
  );
}
