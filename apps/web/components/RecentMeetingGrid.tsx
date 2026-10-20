'use client';

import { useState } from 'react';
import type { MouseEvent as ReactMouseEvent } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import type { MeetingHistoryItem } from '../lib/types';
import MeetingHoverPreview from './MeetingHoverPreview';

const STATUS_LABEL: Record<string, string> = {
  created: '已创建',
  recording: '录音中',
  recorded: '待转录',
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

/**
 * 预览浮层跟随鼠标进入点显示（写入 CSS 变量 --mhp-top），
 * 并夹在卡片范围内避免溢出过多。
 */
function showPreviewAtCursor(event: ReactMouseEvent<HTMLElement>) {
  const el = event.currentTarget;
  const rect = el.getBoundingClientRect();
  const top = event.clientY - rect.top + 16;
  const maxTop = rect.height - 180; // 预估浮层最大高度
  el.style.setProperty('--mhp-top', `${Math.max(10, Math.min(top, Math.max(10, maxTop)))}px`);
}

function TitleEditor({
  item,
  onRename,
}: {
  item: MeetingHistoryItem;
  onRename?: (item: MeetingHistoryItem, title: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');

  if (!editing) {
    return (
      <h3 className="meeting-title">
        {item.title}
        {onRename && (
          <button
            type="button"
            className="title-edit-btn"
            aria-label={`重命名会议「${item.title}」`}
            title="重命名"
            onClick={(event) => {
              event.stopPropagation();
              setDraft(item.title);
              setEditing(true);
            }}
          >✎</button>
        )}
      </h3>
    );
  }
  const commit = () => {
    const title = draft.trim();
    setEditing(false);
    if (title && title !== item.title) onRename?.(item, title);
  };
  return (
    <input
      className="meeting-title-input"
      value={draft}
      autoFocus
      maxLength={200}
      aria-label="编辑会议名称"
      onClick={(event) => event.stopPropagation()}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        event.stopPropagation();
        if (event.key === 'Enter') commit();
        if (event.key === 'Escape') setEditing(false);
      }}
    />
  );
}

export default function RecentMeetingGrid({
  meetings,
  onOpen,
  onDelete,
  onRename,
}: {
  meetings: MeetingHistoryItem[];
  onOpen?: (item: MeetingHistoryItem) => void;
  onDelete?: (item: MeetingHistoryItem) => void;
  onRename?: (item: MeetingHistoryItem, title: string) => void;
}) {
  const router = useRouter();
  // 每页两行（3 列 × 2 行 = 6 张），底部翻页。
  const PAGE_SIZE = 6;
  const pageCount = Math.max(1, Math.ceil(meetings.length / PAGE_SIZE));
  const [page, setPage] = useState(0);
  const safePage = Math.min(page, pageCount - 1);
  const pageItems = meetings.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE);
  const goto = (next: number) => setPage(Math.max(0, Math.min(next, pageCount - 1)));
  return (
    <section className="library-panel recent-panel">
      <div className="section-title">
        <h2>最近的会议</h2>
        <Link className="recent-all-link" href="/history?tab=meeting">查看全部 <span aria-hidden="true">→</span></Link>
      </div>
      {meetings.length === 0 ? (
        <div className="empty-library">
          <span>◉</span>
          <strong>还没有会议纪要记录</strong>
          <p>点击上方「开始」录制一场会议，或导入音频文件，记录会显示在这里。</p>
        </div>
      ) : (
        <>
          <div className="recent-grid">
            {pageItems.map((item) => {
            const duration = formatDuration(item.duration_seconds);
            const deepLink = `/history?tab=meeting&meeting=${item.id}`;
            const openItem = () => {
              if (item.video_id && onOpen) onOpen(item);
              else router.push(deepLink);
            };
            return (
              <article
                className="recent-card meeting-recent-card"
                key={item.id}
                role="button"
                tabIndex={0}
                onClick={openItem}
                onMouseEnter={showPreviewAtCursor}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    openItem();
                  }
                }}
                aria-label={`打开会议「${item.title}」的结果面板`}
              >
                <div className="recent-cover meeting-cover">
                  <span>◉</span>
                  {item.status === 'recording' && <em className="meeting-cover-badge live">录音中</em>}
                  {item.status === 'processing' && <em className="meeting-cover-badge busy"><i className="badge-dot" />转录中</em>}
                  {item.status === 'summarizing' && <em className="meeting-cover-badge busy"><i className="badge-dot" />总结中</em>}
                  {onDelete && (
                    <button
                      type="button"
                      className="recent-delete"
                      aria-label={`删除会议「${item.title}」`}
                      title="删除会议记录"
                      onClick={(event) => {
                        event.stopPropagation();
                        onDelete(item);
                      }}
                    >×</button>
                  )}
                </div>
                <TitleEditor item={item} onRename={onRename} />
                <p>{MODE_LABEL[item.mode] || item.mode}{duration ? ` · ${duration}` : ''}</p>
                <p>{timeLabel(item.updated_at || item.created_at)}</p>
                <p className={`status-text ${item.status === 'processing' || item.status === 'summarizing' ? 'is-busy' : ''} ${item.status === 'failed' ? 'is-failed' : ''}`}>{STATUS_LABEL[item.status] || item.status}</p>
                <p className="flags">
                  <span className={item.has_transcript ? 'flag-on' : 'flag-off'}>{item.has_transcript ? '已有转录' : '无转录'}</span>
                  <span className={item.has_summary ? 'flag-on' : 'flag-off'}>{item.has_summary ? '已有纪要' : '无纪要'}</span>
                </p>
                <MeetingHoverPreview item={item} />
              </article>
            );
          })}
          </div>
          {pageCount > 1 && (
            <nav className="recent-pager" aria-label="会议列表翻页">
              <button type="button" disabled={safePage <= 0} onClick={() => goto(safePage - 1)}>‹ 上一页</button>
              <span>{safePage + 1} / {pageCount}</span>
              <button type="button" disabled={safePage >= pageCount - 1} onClick={() => goto(safePage + 1)}>下一页 ›</button>
            </nav>
          )}
        </>
      )}
    </section>
  );
}
