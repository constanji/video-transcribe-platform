'use client';

import { api, apiRaw } from '../lib/api';
import type { Job, MediaVariant, VideoAsset } from '../lib/types';
import { useEffect, useRef, useState } from 'react';

const STATUS: Record<string, string> = {
  available: '可下载',
  downloaded: '已下载',
  adapter_pending: '待接入',
  failed: '失败',
};

function formatMB(bytes: number) {
  return (bytes / 1024 / 1024).toFixed(1);
}

export default function DownloadAction({
  video,
  variants,
  onToast,
}: {
  video: VideoAsset;
  variants: MediaVariant[];
  onToast: (message: string, kind?: 'success' | 'error') => void;
}) {
  const [variantId, setVariantId] = useState(variants[0]?.id || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [progress, setProgress] = useState<number | null>(null);
  const [loadedBytes, setLoadedBytes] = useState(0);
  const [fetching, setFetching] = useState<{ received: number; total: number } | null>(null);
  const controller = useRef<AbortController | null>(null);
  const jobIdRef = useRef<string | null>(null);

  useEffect(() => {
    setVariantId((current) => variants.some((item) => item.id === current) ? current : variants[0]?.id || '');
  }, [variants]);

  useEffect(() => () => {
    controller.current?.abort();
    if (jobIdRef.current) api(`/api/jobs/${jobIdRef.current}`, { method: 'DELETE' }).catch(() => undefined);
  }, []);

  async function waitForVariant(jobId: string, signal: AbortSignal) {
    // 轮询后台取源任务：进度来自真实字节流（服务器 B站→本地），completed 后文件已落盘
    while (!signal.aborted) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      if (signal.aborted) return;
      const job = await api<Job>(`/api/jobs/${jobId}`, { signal });
      const detail = job.progress_detail || {};
      setFetching({ received: Number(detail.received_bytes || 0), total: Number(detail.total_bytes || 0) });
      if (job.status === 'completed') return;
      if (job.status === 'failed') throw new Error(job.error_message || '视频获取失败');
      if (job.status === 'cancelled') throw Object.assign(new Error('下载已取消'), { name: 'AbortError' });
    }
  }

  async function download() {
    if (!variantId) return;
    setBusy(true);
    setError('');
    setProgress(null);
    setLoadedBytes(0);
    setFetching({ received: 0, total: 0 });
    const currentController = new AbortController();
    controller.current = currentController;
    jobIdRef.current = null;
    try {
      // 阶段 1：后台任务从源站拉视频到服务器（原先是盲区，现在有真实进度）
      try {
        const created = await api<Job>(`/api/videos/${video.id}/download-jobs`, {
          method: 'POST',
          body: JSON.stringify({ variant_id: variantId }),
          signal: currentController.signal,
        });
        if (created.status === 'running' || created.status === 'queued') {
          jobIdRef.current = created.id;
          await waitForVariant(created.id, currentController.signal);
        }
      } catch (err) {
        if ((err as Error).name === 'AbortError') throw err;
        const message = (err as Error).message;
        // 409「已在本地/已有任务」等：文件已就绪时直接进入保存阶段，其余报错终止
        if (!message.includes('本地') && !message.includes('已有下载任务')) throw err;
      }
      if (currentController.signal.aborted) throw Object.assign(new Error('下载已取消'), { name: 'AbortError' });
      setFetching(null);
      // 阶段 2：文件已在服务器本地，回传几乎瞬时，攒 Blob 后交给浏览器保存
      const response = await apiRaw(`/api/videos/${video.id}/download/${variantId}`, { signal: currentController.signal });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error_message || body.detail || '下载失败');
      }
      const total = Number(response.headers.get('Content-Length') || 0);
      const chunks: BlobPart[] = [];
      let received = 0;
      if (response.body) {
        const reader = response.body.getReader();
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          if (value) {
            const copy = new Uint8Array(value.byteLength);
            copy.set(value);
            chunks.push(copy.buffer);
            received += value.byteLength;
            setLoadedBytes(received);
            if (total) setProgress(Math.min(100, Math.round((received / total) * 100)));
          }
        }
      }
      const blob = response.body
        ? new Blob(chunks, { type: response.headers.get('Content-Type') || 'application/octet-stream' })
        : await response.blob();
      const link = document.createElement('a');
      const disposition = response.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename\*=UTF-8''([^;]+)|filename="?([^"]+)"?/);
      link.href = URL.createObjectURL(blob);
      link.download = decodeURIComponent(match?.[1] || match?.[2] || `${video.title || 'video'}.mp4`);
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(link.href), 10_000);
      onToast('已开始在浏览器中保存视频');
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        onToast('下载已取消');
        return;
      }
      const message = (err as Error).message;
      setError(message);
      onToast(message, 'error');
    } finally {
      if (controller.current === currentController) controller.current = null;
      jobIdRef.current = null;
      setBusy(false);
      setFetching(null);
    }
  }

  async function cancelDownload() {
    controller.current?.abort();
    if (jobIdRef.current) {
      await api(`/api/jobs/${jobIdRef.current}`, { method: 'DELETE' }).catch(() => undefined);
    }
  }

  return (
    <div className="action-panel download-panel">
      <div className="download-row">
        <label>
          清晰度 / 格式
          <select value={variantId} onChange={(event) => setVariantId(event.target.value)}>
            {variants.length === 0 && <option value="">暂无可用媒体</option>}
            {variants.map((item) => (
              <option key={item.id} value={item.id}>
                {item.quality || '默认清晰度'} · {item.format.toUpperCase()} · {STATUS[item.download_status] || item.download_status}
              </option>
            ))}
          </select>
        </label>
        {busy ? (
          <button type="button" className="outline-button" onClick={cancelDownload}>
            取消下载
          </button>
        ) : (
          <button type="button" className="primary-button" onClick={download} disabled={!variantId}>
            开始下载
          </button>
        )}
      </div>
      {busy && fetching && (
        <p className="meta-line" role="status">
          {fetching.total
            ? `正在从源站获取视频 ${Math.min(100, Math.round((fetching.received / fetching.total) * 100))}%（${formatMB(fetching.received)} / ${formatMB(fetching.total)} MB）`
            : fetching.received
              ? `正在从源站获取视频，已获取 ${formatMB(fetching.received)} MB`
              : '正在从源站获取视频，请稍候…'}
        </p>
      )}
      {busy && !fetching && (
        <p className="meta-line" role="status">
          {progress != null ? `正在下载 ${progress}%` : `正在下载 ${(loadedBytes / 1024 / 1024).toFixed(1)} MB`}
        </p>
      )}
      {error && (
        <p className="inline-error">
          <span>下载失败：{error}</span>
          <button type="button" className="text-button" onClick={download}>重试</button>
        </p>
      )}
    </div>
  );
}
