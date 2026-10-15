'use client';

import { useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api';
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

export default function TranscriptViewer({
  transcript,
  onToast,
}: {
  transcript?: Transcript;
  onToast?: (message: string, kind?: 'success' | 'error') => void;
}) {
  const [query, setQuery] = useState('');
  const [current, setCurrent] = useState(transcript);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Transcript['segments']>([]);
  const [saving, setSaving] = useState(false);
  const text = current?.text || '';
  const segments = useMemo(
    () => (current?.segments || []).filter((item) => !query || (item.text || '').includes(query)),
    [current, query],
  );

  useEffect(() => setCurrent(transcript), [transcript]);

  if (!current) {
    return <div className="summary-placeholder"><h3>还没有转录结果</h3><p>对该视频发起转录后，文本会显示在这里。</p></div>;
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      onToast?.('转录内容已复制');
    } catch {
      onToast?.('复制失败，请检查浏览器剪贴板权限', 'error');
    }
  }

  function download(kind: 'txt' | 'md') {
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `transcript.${kind === 'md' ? 'md' : 'txt'}`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(link.href), 10_000);
  }

  function renameSpeaker(oldName: string | null | undefined, nextName: string) {
    setDraft((items) => items.map((item) => (
      (item.speaker || '') === (oldName || '') ? { ...item, speaker: nextName } : item
    )));
  }

  async function saveEdit() {
    if (!current || saving) return;
    const nextText = draft.map((item) => (item.text || '').trim()).filter(Boolean).join('\n');
    if (!nextText) {
      onToast?.('转录内容不能为空', 'error');
      return;
    }
    setSaving(true);
    try {
      const revised = await api<Transcript>(`/api/transcripts/${current.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ text: nextText, segments: draft }),
      });
      setCurrent(revised);
      setEditing(false);
      onToast?.('校对结果已保存为新版本');
    } catch (err) {
      onToast?.((err as Error).message, 'error');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="viewer">
      <div className="viewer-toolbar">
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索转录文本" />
        <button type="button" onClick={copy}>复制全文</button>
        <button type="button" onClick={() => download('txt')}>下载 TXT</button>
        <button type="button" onClick={() => download('md')}>下载 Markdown</button>
        {!editing ? (
          <button type="button" onClick={() => {
            const source = current.segments?.length
              ? current.segments
              : current.text.split('\n').filter(Boolean).map((value) => ({ text: value }));
            setDraft(source.map((item) => ({ ...item })));
            setEditing(true);
          }}>校对转录</button>
        ) : (
          <>
            <button type="button" disabled={saving} onClick={saveEdit}>{saving ? '保存中…' : '保存校对版'}</button>
            <button type="button" disabled={saving} onClick={() => setEditing(false)}>取消</button>
          </>
        )}
      </div>
      <div className="viewer-scroll">
        {!editing && !current.segments?.length && <pre className="transcript-text">{text}</pre>}
        <ul className="segments">
          {(editing ? draft : segments).map((item, index) => (
            <li key={index}>
              <time>{clock(segmentStart(item))} - {clock(segmentEnd(item))}</time>
              {editing ? (
                <span>
                  <input
                    className="studio-speaker-input"
                    value={item.speaker || ''}
                    aria-label={`第 ${index + 1} 段说话人`}
                    onChange={(event) => renameSpeaker(item.speaker, event.target.value)}
                  />
                  <textarea
                    className="studio-transcript-editor"
                    value={item.text || ''}
                    aria-label={`第 ${index + 1} 段转录内容`}
                    onChange={(event) => setDraft((items) => items.map((value, itemIndex) => (
                      itemIndex === index ? { ...value, text: event.target.value } : value
                    )))}
                  />
                </span>
              ) : (
                <span>{item.speaker ? `${item.speaker}：` : ''}{item.text}</span>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
