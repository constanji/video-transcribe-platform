'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { meetingSession, useMeetingSession } from '../lib/meeting-session';

function formatClock(ms: number) {
  const total = Math.floor(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

/** 会议会话进行中的全局悬浮条：站内任意页面都能看到进度并控制暂停/停止。 */
export default function MeetingFloatingBar() {
  const state = useMeetingSession();
  const pathname = usePathname();
  const { phase, elapsed, mode, segments } = state;

  // 会议页有自己的完整工作台，不重复展示；空闲态不显示。
  if (pathname === '/' || phase === 'idle' || phase === 'starting' || phase === 'ready') return null;

  const recording = phase === 'recording' || phase === 'paused';
  const statusText =
    phase === 'recording' ? (mode === 'live' ? '实时转录中' : '录音中')
    : phase === 'paused' ? '已暂停'
    : phase === 'stopping' ? '正在保存录音…'
    : phase === 'uploading' ? `正在上传 ${state.uploadProgress ?? 0}%`
    : phase === 'processing' ? '终稿转录中（后台进行，可离开页面）'
    : '';

  return (
    <div className="meeting-dock" role="status" aria-label="会议转录进行中">
      <span className={`meeting-dock-dot ${phase === 'paused' ? 'paused' : phase === 'processing' ? 'busy' : 'live'}`} aria-hidden="true" />
      <span className="meeting-dock-time">{recording ? formatClock(elapsed) : null}</span>
      <span className="meeting-dock-status">
        {statusText}
        {mode === 'live' && recording ? <em> · 已出 {segments.length} 段字幕</em> : null}
      </span>
      <span className="meeting-dock-actions">
        {phase === 'recording' ? (
          <button type="button" onClick={() => meetingSession.togglePause()}>暂停</button>
        ) : null}
        {phase === 'paused' ? (
          <button type="button" className="primary" onClick={() => meetingSession.togglePause()}>继续</button>
        ) : null}
        {recording ? (
          <button type="button" onClick={() => meetingSession.finish()}>停止</button>
        ) : null}
        <Link href="/" className="meeting-dock-back">回到会议 →</Link>
      </span>
    </div>
  );
}
