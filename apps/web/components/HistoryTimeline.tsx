'use client';

import { logoSrc } from '../lib/api';
import type { Platform, VideoAsset } from '../lib/types';

const TYPE_LABEL: Record<string, string> = { parse: '解析', download: '下载', transcribe: '转录', summarize: '总结' };

function groupLabel(date: Date) {
  const today = new Date();
  const start = (value: Date) => new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
  const diff = (start(today) - start(date)) / 86400000;
  if (diff < 1) return '今天';
  if (diff < 2) return '昨天';
  if (diff < 7) return '近一周';
  return '更早';
}

export default function HistoryTimeline({
  items,
  platforms,
  selectedId,
  onSelect,
}: {
  items: VideoAsset[];
  platforms: Platform[];
  selectedId?: string;
  onSelect: (item: VideoAsset) => void;
}) {
  const groups = new Map<string, VideoAsset[]>();
  for (const item of items) {
    const label = groupLabel(new Date(item.last_processed_at || item.created_at));
    groups.set(label, [...(groups.get(label) || []), item]);
  }

  if (items.length === 0) {
    return (
      <div className="empty-state">
        还没有历史解析任务
        <a href="/">去解析一个视频 →</a>
      </div>
    );
  }

  return (
    <>
      {[...groups.entries()].map(([label, videos]) => (
        <div key={label} className="timeline-group">
          <h3>{label}</h3>
          {videos.map((item) => {
            const platform = platforms.find((p) => p.id === item.platform);
            const latest = item.jobs?.[0];
            return (
              <button
                type="button"
                className={`timeline-item ${selectedId === item.id ? 'selected' : ''}`}
                key={item.id}
                onClick={() => onSelect(item)}
              >
                <span className="timeline-dot" />
                <div className="history-thumb">
                  {item.thumbnail_url ? <img src={item.thumbnail_url} alt="" /> : <span>◈</span>}
                </div>
                <div className="history-main">
                  <strong>{item.title}</strong>
                  <small>
                    <img src={logoSrc(platform?.logo)} alt="" width={12} height={12} />
                    {platform?.name || item.platform || '未知平台'} · {TYPE_LABEL[latest?.job_type || 'parse'] || '解析'}
                  </small>
                  <small>
                    {latest?.status || item.parse_status} · {new Date(item.last_processed_at || item.created_at).toLocaleTimeString('zh-CN', { hour12: false })}
                    {item.has_summary ? ' · 总结已完成' : ' · 总结未完成'}
                  </small>
                </div>
              </button>
            );
          })}
        </div>
      ))}
    </>
  );
}
