'use client';

import { logoSrc } from '../lib/api';
import { withToken } from '../lib/auth';
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
  return (
    <section className="library-panel recent-panel">
      <div className="section-title">
        <h2>最近的解析</h2>
        <a className="recent-all-link" href="/history">查看全部 <span aria-hidden="true">→</span></a>
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
                <p className="flags">
                  <span>{video.has_transcript ? '已有转录' : '无转录'}</span>
                  <span>{video.has_summary ? '已有总结' : '无总结'}</span>
                </p>
                <div className="recent-actions">
                  <button type="button" onClick={(event) => { event.stopPropagation(); onDownload(video); }}>视频下载</button>
                  <button type="button" onClick={(event) => { event.stopPropagation(); onSummary(video); }}>转录</button>
                  <a href={`/history?video=${video.id}`} onClick={(event) => event.stopPropagation()}>查看历史任务</a>
                  <a href={`/videos/${video.id}`} onClick={(event) => event.stopPropagation()}>打开详情</a>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
