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

function timeLabel(value?: string | null) {
  if (!value) return '';
  return new Date(value).toLocaleString('zh-CN', { hour12: false });
}

export default function RecentMeetingGrid({ meetings }: { meetings: MeetingHistoryItem[] }) {
  return (
    <section className="library-panel recent-panel">
      <div className="section-title">
        <h2>最近的会议</h2>
        <a className="recent-all-link" href="/history?tab=meeting">查看全部 <span aria-hidden="true">→</span></a>
      </div>
      {meetings.length === 0 ? (
        <div className="empty-library">
          <span>◉</span>
          <strong>还没有会议纪要记录</strong>
          <p>点击上方「开始」录制一场会议，或导入音频文件，记录会显示在这里。</p>
        </div>
      ) : (
        <div className="recent-grid">
          {meetings.map((item) => {
            const duration = formatDuration(item.duration_seconds);
            return (
              <a
                className="recent-card meeting-recent-card"
                key={item.id}
                href={`/history?tab=meeting&meeting=${item.id}`}
                aria-label={`查看会议「${item.title}」的纪要详情`}
              >
                <div className="recent-cover meeting-cover">
                  <span>◉</span>
                  {item.status === 'recording' && <em className="meeting-cover-badge live">录音中</em>}
                  {(item.status === 'processing' || item.status === 'summarizing') && <em className="meeting-cover-badge busy">转录中</em>}
                </div>
                <h3>{item.title}</h3>
                <p>{MODE_LABEL[item.mode] || item.mode}{duration ? ` · ${duration}` : ''}</p>
                <p>{timeLabel(item.updated_at || item.created_at)}</p>
                <p className="status-text">{STATUS_LABEL[item.status] || item.status}</p>
                <p className="flags">
                  <span>{item.has_transcript ? '已有转录' : '无转录'}</span>
                  <span>{item.has_summary ? '已有纪要' : '无纪要'}</span>
                </p>
              </a>
            );
          })}
        </div>
      )}
    </section>
  );
}
