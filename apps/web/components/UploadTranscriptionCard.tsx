'use client';

import { DragEvent, useRef, useState } from 'react';
import { uploadFile } from '../lib/api';
import type { UploadResponse } from '../lib/types';

const ACCEPT = '.mp3,.wav,.m4a,.mp4,.webm,.mov';
const MAX_BYTES = 500 * 1024 * 1024;

function formatSize(bytes: number) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function UploadTranscriptionCard({
  onUploaded,
  onToast,
}: {
  onUploaded: (result: UploadResponse) => void;
  onToast: (message: string, kind?: 'success' | 'error') => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [progress, setProgress] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const controllerRef = useRef<AbortController | null>(null);

  function choose(next?: File) {
    if (!next) return;
    const extension = `.${next.name.split('.').pop()?.toLowerCase() || ''}`;
    if (!ACCEPT.split(',').includes(extension)) { setError('仅支持 MP3、WAV、M4A、MP4、WebM 和 MOV 文件'); return; }
    if (next.size > MAX_BYTES) { setError('单个文件不能超过 500MB'); return; }
    if (!next.size) { setError('不能上传空文件'); return; }
    setError(''); setProgress(0); setFile(next);
  }

  async function upload() {
    if (!file || busy) return;
    setBusy(true); setError(''); setProgress(0);
    const controller = new AbortController();
    controllerRef.current = controller;
    try {
      const result = await uploadFile<UploadResponse>(file, setProgress, controller.signal);
      onUploaded(result);
      onToast(result.duplicate ? '已找到相同文件，已恢复原有资产' : '文件上传成功，请选择语言后开始转录');
      setFile(null); setProgress(100);
    } catch (err) {
      const message = (err as Error).message;
      if (message !== '上传已取消') { setError(message); onToast(message, 'error'); }
    } finally { setBusy(false); controllerRef.current = null; }
  }

  function drop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault(); setDragging(false); choose(event.dataTransfer.files[0]);
  }

  return (
    <section className="upload-card workspace-card">
      <div className="workspace-heading"><div><h2>上传转录</h2><p>上传本机音频或视频，完成后在结果卡中手动开始转录。</p></div><span className="upload-limit">最大 500MB</span></div>
      <div className={`upload-dropzone ${dragging ? 'is-dragging' : ''}`} onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={drop}>
        <input ref={inputRef} type="file" accept={ACCEPT} hidden onChange={(event) => choose(event.target.files?.[0])} />
        <span className="upload-icon">↑</span>
        <strong>{file ? file.name : '拖拽文件到这里，或点击选择'}</strong>
        <small>支持 MP3、WAV、M4A、MP4、WebM、MOV</small>
        <button type="button" className="outline-button" onClick={() => inputRef.current?.click()} disabled={busy}>选择文件</button>
      </div>
      {file && <div className="upload-file-row"><div><strong>{file.name}</strong><span>{formatSize(file.size)} · {file.type || '媒体文件'}</span></div><div className="upload-actions"><button type="button" className="text-button" onClick={() => setFile(null)} disabled={busy}>移除</button><button type="button" className="primary-button" onClick={upload} disabled={busy}>{busy ? `上传中 ${progress}%` : '开始上传'}</button></div></div>}
      {busy && <div className="upload-progress"><i style={{ width: `${progress}%` }} /></div>}
      {busy && <button type="button" className="text-button upload-cancel" onClick={() => controllerRef.current?.abort()}>取消上传</button>}
      {error && <p className="inline-error">{error}</p>}
    </section>
  );
}
