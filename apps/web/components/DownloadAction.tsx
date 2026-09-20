'use client';

import { API } from '../lib/api';
import type { MediaVariant, VideoAsset } from '../lib/types';
import { useEffect, useState } from 'react';

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

  useEffect(() => {
    setVariantId(variants[0]?.id || '');
  }, [variants]);

  async function download() {
    if (!variantId) return;
    setBusy(true);
    setError('');
    try {
      const response = await fetch(`${API}/api/videos/${video.id}/download/${variantId}`);
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error_message || body.detail || '下载失败');
      }
      const blob = await response.blob();
      const link = document.createElement('a');
      const disposition = response.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename\*=UTF-8''([^;]+)|filename="?([^"]+)"?/);
      link.href = URL.createObjectURL(blob);
      link.download = decodeURIComponent(match?.[1] || match?.[2] || `${video.title || 'video'}.mp4`);
      link.click();
      URL.revokeObjectURL(link.href);
      onToast('已开始在浏览器中保存视频');
    } catch (err) {
      const message = (err as Error).message;
      setError(message);
      onToast(message, 'error');
    } finally {
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
        <button type="button" className="primary-button" onClick={download} disabled={busy || !variantId}>
          {busy ? '正在准备…' : '开始下载'}
        </button>
      </div>
      {error && (
        <p className="inline-error">
          <span>下载失败：{error}</span>
          <button type="button" className="text-button" onClick={download}>重试</button>
        </p>
      )}
    </div>
  );
}
