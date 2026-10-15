'use client';

import { useEffect, useState } from 'react';
import { api, logoSrc } from '../lib/api';
import { withToken } from '../lib/auth';
import { isImageRecord } from '../lib/media-kind';
import type { Platform, VideoAsset } from '../lib/types';

type MediaFilter = 'all' | 'video' | 'image';

const MEDIA_ORDER: MediaFilter[] = ['all', 'video', 'image'];

function timeLabel(value?: string | null) {
  if (!value) return '尚未处理';
  return new Date(value).toLocaleString('zh-CN', { hour12: false });
}

export default function RecentVideoGrid({
  videos,
  platforms,
  onDownload,
  onSummary,
  onOpen,
  onDelete,
}: {
  videos: VideoAsset[];
  platforms: Platform[];
  onDownload: (video: VideoAsset) => void;
  onSummary: (video: VideoAsset) => void;
  onOpen: (video: VideoAsset) => void;
  onDelete: (video: VideoAsset) => void;
}) {
  const [media, setMedia] = useState<MediaFilter>('all');
  const [slideDir, setSlideDir] = useState<'next' | 'prev'>('next');
  // 按类型缓存结果（stale-while-revalidate）：切换时先显示旧内容，后台刷新，避免闪烁
  const [scopedCache, setScopedCache] = useState<{ sig: VideoAsset[]; items: Partial<Record<MediaFilter, VideoAsset[]>> }>({ sig: [], items: {} });

  useEffect(() => {
    if (media === 'all') return;
    if (scopedCache.sig === videos && scopedCache.items[media]) return;
    let active = true;
    api<VideoAsset[]>(`/api/videos/recent?media=${media}`).then((data) => {
      if (active) setScopedCache((prev) => ({ sig: videos, items: { ...prev.items, [media]: data } }));
    }).catch(() => {
      if (active) setScopedCache((prev) => ({ sig: videos, items: { ...prev.items, [media]: [] } }));
    });
    return () => { active = false; };
  }, [media, videos, scopedCache]);

  const shown = media === 'all' ? videos : scopedCache.items[media] ?? null;
  const historyHref = media === 'all' ? '/history' : `/history?media=${media}`;

  const switchMedia = (next: MediaFilter) => {
    if (next === media) return;
    setSlideDir(MEDIA_ORDER.indexOf(next) > MEDIA_ORDER.indexOf(media) ? 'next' : 'prev');
    setMedia(next);
  };

  return (
    <section className="library-panel recent-panel">
      <div className="section-title">
        <h2>最近的解析</h2>
        <div className="media-switch" role="tablist" aria-label="解析记录类型">
          {([['all', '全部'], ['video', '视频'], ['image', '图片']] as const).map(([id, label]) => (
            <button type="button" key={id} className={media === id ? 'active' : ''} onClick={() => switchMedia(id)}>{label}</button>
          ))}
        </div>
        <a className="recent-all-link" href={historyHref}>查看全部 <span aria-hidden="true">→</span></a>
      </div>
      <div className="recent-swap" key={media} data-dir={slideDir}>
        {shown === null ? (
          <div className="empty-library"><strong>正在加载</strong></div>
        ) : shown.length === 0 ? (
        <div className="empty-library">
          <span>▣</span>
          <strong>{media === 'image' ? '还没有图片解析记录' : media === 'video' ? '还没有视频解析记录' : '还没有解析记录'}</strong>
          <p>粘贴链接并解析后，封面、标题和状态会显示在这里。</p>
        </div>
      ) : (
        <div className="recent-grid">
          {shown.map((video) => {
            const platform = platforms.find((item) => item.id === video.platform);
            const imageOnly = isImageRecord(video);
            return (
              <article className="recent-card" key={video.id} role="button" tabIndex={0} onClick={() => onOpen(video)} onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  onOpen(video);
                }
              }} aria-label={`打开${video.title}的解析结果`}>
                <div className="recent-cover">
                  {video.thumbnail_url ? <img src={withToken(video.thumbnail_url)} alt="" /> : <span>◈</span>}
                  <button type="button" className="recent-delete" aria-label={`删除${video.title}`} title="删除解析记录" onClick={(event) => {
                    event.stopPropagation();
                    onDelete(video);
                  }}>×</button>
                </div>
                <h3>{video.title}</h3>
                <p>
                  <img src={logoSrc(platform?.logo)} alt="" width={14} height={14} />
                  {platform?.name || video.platform || '未知平台'}
                </p>
                <p>{timeLabel(video.last_processed_at || video.created_at)}</p>
                <p className="status-text">{video.parse_status === 'uploaded' ? '文件已就绪' : video.parse_status === 'parsed' ? '已解析' : '待接入解析器'}</p>
                {!imageOnly && (
                  <p className="flags">
                    <span className={video.has_transcript ? 'flag-on' : 'flag-off'}>{video.has_transcript ? '已有转录' : '无转录'}</span>
                    <span className={video.has_summary ? 'flag-on' : 'flag-off'}>{video.has_summary ? '已有总结' : '无总结'}</span>
                  </p>
                )}
                <div className="recent-actions">
                  {imageOnly ? (
                    <button type="button" onClick={(event) => { event.stopPropagation(); onOpen(video); }}>查看图片</button>
                  ) : (
                    <>
                      <button type="button" onClick={(event) => { event.stopPropagation(); onDownload(video); }}>视频下载</button>
                      <button type="button" onClick={(event) => { event.stopPropagation(); onSummary(video); }}>转录</button>
                    </>
                  )}
                  <a href={`/history?video=${video.id}&media=${imageOnly ? 'image' : 'video'}`} onClick={(event) => event.stopPropagation()}>查看历史任务</a>
                  <a href={`/videos/${video.id}`} onClick={(event) => event.stopPropagation()}>打开详情</a>
                </div>
              </article>
            );
          })}
        </div>
      )}
      </div>
    </section>
  );
}
