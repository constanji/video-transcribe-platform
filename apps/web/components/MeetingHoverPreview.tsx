'use client';

import type { MeetingHistoryItem } from '../lib/types';

/**
 * 会议卡片的悬停预览浮层：展示转录/纪要开头摘录。
 * 纯 CSS 控制显隐（父级 :hover / :focus-within），无 JS 状态。
 * 全部用 span + class（不用 div/p），保证能放进 <button> 类卡片里。
 */
export default function MeetingHoverPreview({ item }: { item: MeetingHistoryItem }) {
  const transcript = (item.transcript_preview || '').trim();
  const summary = (item.summary_preview || '').trim();
  return (
    <span className="meeting-hover-preview" role="presentation" aria-hidden="true">
      <span className="mhp-block">
        <em className="mhp-label">转录摘录</em>
        <span className="mhp-text">{transcript || '暂无转录内容'}</span>
      </span>
      <span className="mhp-block">
        <em className="mhp-label">纪要摘录</em>
        <span className="mhp-text">{summary || '暂无纪要内容'}</span>
      </span>
    </span>
  );
}
