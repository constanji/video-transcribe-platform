'use client';

import { apiRaw } from '../lib/api';
import type { MediaVariant, VideoAsset } from '../lib/types';
import { useEffect, useRef, useState } from 'react';

const STATUS: Record<string, string> = {
  available: '可下载',
  downloaded: '已下载',
  adapter_pending: '待接入',
  failed: '失败',
};

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
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    setVariantId((current) => variants.some((item) => item.id === current) ? current : variants[0]?.id || '');
  }, [variants]);

  useEffect(() => () => controller.current?.abort(), []);

  async function download() {
    if (!variantId) return;
    setBusy(true);
    setError('');
    setProgress(null);
    setLoadedBytes(0);
    const currentController = new AbortController();
    controller.current = currentController;
    try {
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
      setBusy(false);
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
          <button type="button" className="outline-button" onClick={() => controller.current?.abort()}>
            取消下载
          </button>
        ) : (
          <button type="button" className="primary-button" onClick={download} disabled={!variantId}>
            开始下载
          </button>
        )}
      </div>
      {busy && (
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
