'use client';

import { logoSrc } from '../lib/api';
import type { Platform, VideoAsset } from '../lib/types';

function timeLabel(value?: string | null) {
  if (!value) return '尚未处理';
  return new Date(value).toLocaleString('zh-CN', { hour12: false });
}

export default function RecentVideoGrid({
  videos,
  platforms,
  onDownload,
  onSummary,
}: {
  videos: VideoAsset[];
  platforms: Platform[];
  onDownload: (video: VideoAsset) => void;
  onSummary: (video: VideoAsset) => void;
}) {
  return (
    <section className="library-panel recent-panel">
      <div className="section-title">
        <h2>最近的解析</h2>
        <a className="text-button" href="/history">查看全部 →</a>
      </div>
      {videos.length === 0 ? (
        <div className="empty-library">
          <span>▣</span>
          <strong>还没有解析记录</strong>
          <p>粘贴链接并解析后，封面、标题和状态会显示在这里。</p>
        </div>
      ) : (
        <div className="recent-grid">
          {videos.map((video) => {
            const platform = platforms.find((item) => item.id === video.platform);
            return (
              <article className="recent-card" key={video.id}>
                <div className="recent-cover">
                  {video.thumbnail_url ? <img src={video.thumbnail_url} alt="" /> : <span>◈</span>}
                </div>
                <h3>{video.title}</h3>
                <p>
                  <img src={logoSrc(platform?.logo)} alt="" width={14} height={14} />
                  {platform?.name || video.platform || '未知平台'}
                </p>
                <p>{timeLabel(video.last_processed_at || video.created_at)}</p>
                <p className="status-text">{video.parse_status === 'parsed' ? '已解析' : '待接入解析器'}</p>
                <p className="flags">
                  <span>{video.has_transcript ? '已有转录' : '无转录'}</span>
                  <span>{video.has_summary ? '已有总结' : '无总结'}</span>
                </p>
                <div className="recent-actions">
                  <button type="button" onClick={() => onDownload(video)}>视频下载</button>
                  <button type="button" onClick={() => onSummary(video)}>转录</button>
                  <a href={`/history?video=${video.id}`}>查看历史任务</a>
                  <a href={`/videos/${video.id}`}>打开详情</a>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
