'use client';

import { useEffect, useState } from 'react';
import { API, apiRaw, logoSrc } from '../lib/api';
import { withToken } from '../lib/auth';
import type { MediaVariant, Platform, VideoAsset } from '../lib/types';

function previewSrc(videoId: string, variantId: string) {
  return withToken(`${API}/api/videos/${videoId}/preview/${variantId}`) || '';
}

async function saveResponse(response: Response, fallback: string) {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || body.error_message || '下载失败');
  }
  const blob = await response.blob();
  const link = document.createElement('a');
  const disposition = response.headers.get('Content-Disposition') || '';
  const match = disposition.match(/filename\*=UTF-8''([^;]+)|filename="?([^"]+)"?/);
  link.href = URL.createObjectURL(blob);
  link.download = decodeURIComponent(match?.[1] || match?.[2] || fallback);
  link.click();
  URL.revokeObjectURL(link.href);
}

export default function ImageGallery({
  id,
  video,
  platform,
  images,
  onReparse,
  onToast,
}: {
  id?: string;
  video: VideoAsset;
  platform?: Platform;
  images: MediaVariant[];
  onReparse: () => void;
  onToast: (message: string, kind?: 'success' | 'error') => void;
}) {
  const [index, setIndex] = useState(0);
  const [picked, setPicked] = useState<string[]>(() => images.map((item) => item.id));
  const [zoomed, setZoomed] = useState(false);
  const [busy, setBusy] = useState('');
  const current = images[Math.min(index, Math.max(images.length - 1, 0))];
  const multiple = images.length > 1;

  useEffect(() => {
    setIndex(0);
    setPicked(images.map((item) => item.id));
    setZoomed(false);
  }, [video.id]);

  useEffect(() => {
    if (!zoomed) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') setZoomed(false);
      if (event.key === 'ArrowLeft') setIndex((value) => (value - 1 + images.length) % images.length);
      if (event.key === 'ArrowRight') setIndex((value) => (value + 1) % images.length);
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [zoomed, images.length]);

  function step(delta: number) {
    setIndex((value) => (value + delta + images.length) % images.length);
  }

  function toggle(variantId: string) {
    setPicked((currentIds) => (
      currentIds.includes(variantId) ? currentIds.filter((item) => item !== variantId) : [...currentIds, variantId]
    ));
  }

  if (!current) return null;

  async function downloadCurrent() {
    setBusy('current');
    try {
      const response = await apiRaw(`/api/videos/${video.id}/download/${current.id}`);
      await saveResponse(response, `${video.title || 'image'}.${current.format || 'jpg'}`);
      onToast('已开始保存当前图片');
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setBusy('');
    }
  }

  async function downloadZip(ids: string[], label: string) {
    if (!ids.length) return;
    setBusy(label);
    try {
      const response = await apiRaw(`/api/videos/${video.id}/download-zip`, {
        method: 'POST',
        body: JSON.stringify({ variant_ids: ids }),
      });
      await saveResponse(response, `${video.title || 'images'}.zip`);
      onToast(label === 'all' ? '已开始下载全部图片' : `已开始下载 ${ids.length} 张图片`);
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setBusy('');
    }
  }

  return (
    <section id={id} className="result-card image-result">
      <div className="result-top">
        <span className="platform-badge">
          <img src={logoSrc(platform?.logo)} alt="" width={16} height={16} />
          {platform?.name || video.platform || '图片'}
        </span>
        <div className="result-top-right">
          <span className="status-text parsed">解析完成</span>
          <button type="button" className="text-button" onClick={onReparse}>重新解析</button>
        </div>
      </div>
      <h2>{video.title || '未命名图片'}</h2>
      <p className="meta-line">
        {video.author || '作者信息待获取'}
        <i />
        共 {images.length} 张
        {multiple && <><i />第 {index + 1} 张</>}
      </p>
      {video.source_url && (
        <a className="source-link" href={video.source_url} target="_blank" rel="noreferrer">{video.source_url}</a>
      )}
      <div className="image-stage">
        {multiple && <button type="button" className="image-nav prev" onClick={() => step(-1)} aria-label="上一张">‹</button>}
        <button type="button" className="image-frame" onClick={() => setZoomed(true)} aria-label="放大查看">
          <img src={previewSrc(video.id, current.id)} alt={video.title || '图片'} />
        </button>
        {multiple && <button type="button" className="image-nav next" onClick={() => step(1)} aria-label="下一张">›</button>}
      </div>
      <div className="image-thumbs" role="list">
        {images.map((item, itemIndex) => (
          <div className={`image-thumb ${itemIndex === index ? 'is-current' : ''}`} key={item.id} role="listitem">
            <label className="image-check">
              <input type="checkbox" checked={picked.includes(item.id)} onChange={() => toggle(item.id)} aria-label={`选择第 ${itemIndex + 1} 张`} />
            </label>
            <button type="button" onClick={() => setIndex(itemIndex)}>
              <img src={previewSrc(video.id, item.id)} alt="" />
            </button>
          </div>
        ))}
      </div>
      <div className="result-actions">
        <button type="button" className="primary-button" onClick={downloadCurrent} disabled={Boolean(busy)}>
          {busy === 'current' ? '正在准备…' : '下载当前图片'}
        </button>
        <button type="button" className="outline-button" onClick={() => downloadZip(picked, 'batch')} disabled={Boolean(busy) || picked.length === 0}>
          {busy === 'batch' ? '正在打包…' : `批量下载（${picked.length}）`}
        </button>
        <button type="button" className="outline-button" onClick={() => downloadZip(images.map((item) => item.id), 'all')} disabled={Boolean(busy)}>
          {busy === 'all' ? '正在打包…' : '全部下载'}
        </button>
      </div>
      {zoomed && (
        <div className="image-lightbox" role="dialog" aria-modal="true" aria-label="查看大图" onClick={() => setZoomed(false)}>
          {multiple && <button type="button" className="image-nav prev" onClick={(event) => { event.stopPropagation(); step(-1); }} aria-label="上一张">‹</button>}
          <img src={previewSrc(video.id, current.id)} alt={video.title || '图片'} onClick={(event) => event.stopPropagation()} />
          {multiple && <button type="button" className="image-nav next" onClick={(event) => { event.stopPropagation(); step(1); }} aria-label="下一张">›</button>}
          <button type="button" className="image-lightbox-close" onClick={() => setZoomed(false)} aria-label="关闭">×</button>
        </div>
      )}
    </section>
  );
}
