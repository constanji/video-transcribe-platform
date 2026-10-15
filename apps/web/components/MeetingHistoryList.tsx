'use client';

import type { MeetingHistoryItem } from '../lib/types';

const STATUS_LABEL: Record<string, string> = {
  created: '已创建',
  recording: '录音中',
  processing: '转录中',
  summarizing: '总结中',
  ready: '已完成',
  failed: '失败',
};

const MODE_LABEL: Record<string, string> = {
  live: '实时转写',
  recording: '录音导入',
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

function formatDuration(seconds?: number | null) {
  if (!seconds || seconds <= 0) return '';
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(s).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

export default function MeetingHistoryList({
  items,
  selectedId,
  onSelect,
}: {
  items: MeetingHistoryItem[];
  selectedId?: string;
  onSelect: (item: MeetingHistoryItem) => void;
}) {
  const groups = new Map<string, MeetingHistoryItem[]>();
  for (const item of items) {
    const label = groupLabel(new Date(item.updated_at || item.created_at));
    groups.set(label, [...(groups.get(label) || []), item]);
  }

  if (items.length === 0) {
    return (
      <div className="empty-state">
        还没有会议纪要记录
        <a href="/">去创建一个会议 →</a>
      </div>
    );
  }

  return (
    <>
      {[...groups.entries()].map(([label, meetings]) => (
        <div key={label} className="timeline-group">
          <h3>{label}</h3>
          {meetings.map((item) => (
            <button
              type="button"
              className={`timeline-item ${selectedId === item.id ? 'selected' : ''}`}
              key={item.id}
              onClick={() => onSelect(item)}
            >
              <span className="timeline-dot" />
              <div className="history-thumb">
                <span>◈</span>
              </div>
              <div className="history-main">
                <strong className="history-title">{item.title}</strong>
                <small className="history-meta">
                  会议纪要 · {MODE_LABEL[item.mode] || item.mode}
                  {formatDuration(item.duration_seconds) ? ` · ${formatDuration(item.duration_seconds)}` : ''}
                </small>
                <div className="history-tags">
                  <em className={`history-tag ${item.status === 'processing' || item.status === 'summarizing' ? 'busy' : item.status === 'ready' ? 'ok' : item.status === 'failed' ? 'err' : ''}`}>
                    {(item.status === 'processing' || item.status === 'summarizing') && <i className="badge-dot" />}
                    {STATUS_LABEL[item.status] || item.status}
                  </em>
                  <em className={`history-tag ${item.has_transcript ? 'flag-on' : 'flag-off'}`}>{item.has_transcript ? '已转录' : '未转录'}</em>
                  <em className={`history-tag ${item.has_summary ? 'flag-on' : 'flag-off'}`}>{item.has_summary ? '纪要完成' : '纪要未完成'}</em>
                </div>
              </div>
            </button>
          ))}
        </div>
      ))}
    </>
  );
}
