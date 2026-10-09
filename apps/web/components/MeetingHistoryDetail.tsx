'use client';

import { useEffect, useState } from 'react';
import { getMeeting } from '../lib/meeting-api';
import type { MeetingRecord, Template } from '../lib/types';
import SummaryViewer from './SummaryViewer';
import TranscriptViewer from './TranscriptViewer';

const STATUS_LABEL: Record<string, string> = {
  created: '已创建',
  recording: '录音中',
  processing: '转录中',
  summarizing: '总结中',
  ready: '已完成',
  failed: '失败',
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

export default function MeetingHistoryDetail({
  meetingId,
  templates,
  onToast,
}: {
  meetingId?: string;
  templates: Template[];
  onToast: (message: string, kind?: 'success' | 'error') => void;
}) {
  const [meeting, setMeeting] = useState<MeetingRecord>();
  const [tab, setTab] = useState<'summary' | 'transcript'>('summary');
  const [error, setError] = useState('');

  useEffect(() => {
    if (!meetingId) {
      setMeeting(undefined);
      return;
    }
    setMeeting(undefined);
    setError('');
    getMeeting(meetingId)
      .then((data) => setMeeting(data))
      .catch(() => setError('会议详情加载失败'));
  }, [meetingId]);

  if (!meetingId) return <div className="empty-state">选择左侧会议查看详情</div>;
  if (error) return <div className="empty-state">{error}</div>;
  if (!meeting) return <div className="empty-state">加载中…</div>;

  const summary = meeting.summary ? [meeting.summary] : [];
  const transcript = meeting.transcript && meeting.transcript.text?.trim() ? meeting.transcript : undefined;

  return (
    <>
      <div className="detail-hero">
        <div className="detail-cover">
          <span>◈</span>
        </div>
        <div>
          <span className="platform-badge">会议纪要</span>
          <h2>{meeting.title}</h2>
          <p>
            {STATUS_LABEL[meeting.status] || meeting.status}
            {formatDuration(meeting.video?.duration_seconds) ? ` · ${formatDuration(meeting.video?.duration_seconds)}` : ''}
            {` · ${new Date(meeting.created_at).toLocaleString('zh-CN', { hour12: false })}`}
          </p>
          {meeting.error_message && <p className="inline-error">{meeting.error_message}</p>}
        </div>
      </div>
      <div className="detail-tabs">
        <button type="button" className={tab === 'summary' ? 'active' : ''} onClick={() => setTab('summary')}>纪要</button>
        <button type="button" className={tab === 'transcript' ? 'active' : ''} onClick={() => setTab('transcript')}>转录</button>
      </div>
      {tab === 'summary' && (
        <SummaryViewer summaries={summary} templates={templates} onRetry={() => onToast('请到会议工作台重新生成纪要')} onToast={onToast} />
      )}
      {tab === 'transcript' && <TranscriptViewer transcript={transcript} onToast={onToast} />}
    </>
  );
}
