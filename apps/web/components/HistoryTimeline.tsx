'use client';

import { logoSrc } from '../lib/api';
import { withToken } from '../lib/auth';
import { isImageRecord } from '../lib/media-kind';
import type { Platform, VideoAsset } from '../lib/types';

const TYPE_LABEL: Record<string, string> = { parse: '解析', download: '下载', transcribe: '转录', summarize: '总结' };

const JOB_STATUS: Record<string, { label: string; kind: string }> = {
  queued: { label: '排队中', kind: 'busy' },
  running: { label: '进行中', kind: 'busy' },
  completed: { label: '已完成', kind: 'ok' },
  failed: { label: '失败', kind: 'err' },
  cancelled: { label: '已取消', kind: '' },
};

const PARSE_STATUS: Record<string, { label: string; kind: string }> = {
  parsed: { label: '已解析', kind: 'ok' },
  uploaded: { label: '文件就绪', kind: 'busy' },
  pending: { label: '待解析', kind: '' },
  failed: { label: '解析失败', kind: 'err' },
};

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
  emptyText,
}: {
  items: VideoAsset[];
  platforms: Platform[];
  selectedId?: string;
  onSelect: (item: VideoAsset) => void;
  emptyText?: string;
}) {
  const groups = new Map<string, VideoAsset[]>();
  for (const item of items) {
    const label = groupLabel(new Date(item.last_processed_at || item.created_at));
    groups.set(label, [...(groups.get(label) || []), item]);
  }

  if (items.length === 0) {
    return (
      <div className="empty-state">
        {emptyText || '还没有历史解析任务'}
        <a href="/parse">去解析 →</a>
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
            const image = isImageRecord(item);
            return (
              <button
                type="button"
                className={`timeline-item ${selectedId === item.id ? 'selected' : ''}`}
                key={item.id}
                onClick={() => onSelect(item)}
              >
                <span className="timeline-dot" />
                <div className="history-thumb">
                  {item.thumbnail_url ? <img src={withToken(item.thumbnail_url)} alt="" /> : <span>◈</span>}
                </div>
                <div className="history-main">
                  <strong className="history-title">{item.title}</strong>
                  <small className="history-meta">
                    <img src={logoSrc(platform?.logo)} alt="" width={12} height={12} />
                    {platform?.name || item.platform || '未知平台'} · {TYPE_LABEL[latest?.job_type || 'parse'] || '解析'}
                  </small>
                  <div className="history-tags">
                    {(() => {
                      const raw = latest?.status || item.parse_status;
                      const info = (latest ? JOB_STATUS : PARSE_STATUS)[raw] || { label: raw || '—', kind: '' };
                      return <em className={`history-tag ${info.kind}`}>{info.label}</em>;
                    })()}
                    <em className="history-tag time">
                      {new Date(item.last_processed_at || item.created_at).toLocaleTimeString('zh-CN', { hour12: false })}
                    </em>
                    {image
                      ? <em className="history-tag">图片</em>
                      : <em className={`history-tag ${item.has_summary ? 'flag-on' : 'flag-off'}`}>{item.has_summary ? '已总结' : '未总结'}</em>}
                  </div>
                </div>
              </button>
            );
          })}
        </div>
      ))}
    </>
  );
}
