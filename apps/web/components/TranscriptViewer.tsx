'use client';

import { useMemo, useState } from 'react';
import type { Transcript } from '../lib/types';

function segmentStart(item: Transcript['segments'][number]) {
  if (typeof item.start_ms === 'number') return item.start_ms;
  if (typeof item.start === 'number') return item.start > 1000 ? item.start : item.start * 1000;
  return 0;
}

function segmentEnd(item: Transcript['segments'][number]) {
  if (typeof item.end_ms === 'number') return item.end_ms;
  if (typeof item.end === 'number') return item.end > 1000 ? item.end : item.end * 1000;
  return segmentStart(item);
}

function clock(ms: number) {
  const sec = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(sec / 60);
  const rest = sec % 60;
  return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
}

export default function TranscriptViewer({ transcript }: { transcript?: Transcript }) {
  const [query, setQuery] = useState('');
  const text = transcript?.text || '';
  const segments = useMemo(
    () => (transcript?.segments || []).filter((item) => !query || (item.text || '').includes(query)),
    [transcript, query],
  );

  if (!transcript) {
    return <div className="summary-placeholder"><h3>还没有转录结果</h3><p>对该视频发起转录后，文本会显示在这里。</p></div>;
  }

  async function copy() {
    await navigator.clipboard.writeText(text);
  }

  function download(kind: 'txt' | 'md') {
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `transcript.${kind === 'md' ? 'md' : 'txt'}`;
    link.click();
  }

  return (
    <div className="viewer">
      <div className="viewer-toolbar">
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索转录文本" />
        <button type="button" onClick={copy}>复制全文</button>
        <button type="button" onClick={() => download('txt')}>下载 TXT</button>
        <button type="button" onClick={() => download('md')}>下载 Markdown</button>
      </div>
      <div className="viewer-scroll">
        <pre className="transcript-text">{text}</pre>
        <ul className="segments">
          {segments.map((item, index) => (
            <li key={index}>
              <time>{clock(segmentStart(item))} - {clock(segmentEnd(item))}</time>
              <span>{item.speaker ? `${item.speaker}：` : ''}{item.text}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
