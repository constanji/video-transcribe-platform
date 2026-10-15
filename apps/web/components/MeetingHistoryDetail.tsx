'use client';

import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import { getMeeting, retryTranscribe } from '../lib/meeting-api';
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
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!meetingId) {
      setMeeting(undefined);
      return;
    }
    setMeeting(undefined);
    setError('');
    let cancelled = false;
    const load = () => getMeeting(meetingId)
      .then((data) => {
        if (!cancelled) {
          setMeeting(data);
          setError('');
        }
        return data;
      })
      .catch(() => {
        if (!cancelled) setError('会议详情加载失败');
        return undefined;
      });
    void load();
    const timer = window.setInterval(() => {
      if (cancelled) return;
      void load();
    }, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [meetingId]);

  if (!meetingId) return <div className="empty-state">选择左侧会议查看详情</div>;
  if (error) return <div className="empty-state">{error}</div>;
  if (!meeting) return <div className="empty-state">加载中…</div>;

  const summary = meeting.summary ? [meeting.summary] : [];
  const transcript = meeting.transcript && meeting.transcript.text?.trim() ? meeting.transcript : undefined;

  async function retryFinal() {
    if (!meetingId || busy) return;
    setBusy(true);
    try {
      setMeeting(await retryTranscribe(meetingId));
      setTab('transcript');
      onToast('已重新启动终稿转录');
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setBusy(false);
    }
  }

  async function generateSummary() {
    if (!meetingId || busy || !transcript) return;
    const preferred = templates.find((item) => item.is_default)
      || templates.find((item) => item.name === '标准会议纪要')
      || templates[0];
    setBusy(true);
    try {
      setMeeting(await api<MeetingRecord>(`/api/v1/meetings/${meetingId}/summaries`, {
        method: 'POST',
        body: JSON.stringify({ template_id: preferred?.id || null }),
      }));
      setTab('summary');
      onToast('正在生成会议纪要');
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setBusy(false);
    }
  }

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
          <div className="detail-actions">
            {(meeting.status === 'failed' || !transcript) && (
              <button type="button" className="outline-button" disabled={busy || meeting.status === 'processing'} onClick={retryFinal}>
                {meeting.status === 'processing' ? '转录中…' : '重试终稿转录'}
              </button>
            )}
            {transcript && (
              <button type="button" className="primary-button" disabled={busy || meeting.status === 'summarizing'} onClick={generateSummary}>
                {meeting.status === 'summarizing' ? '纪要生成中…' : meeting.summary ? '重新生成纪要' : '生成纪要'}
              </button>
            )}
          </div>
        </div>
      </div>
      <div className="detail-tabs">
        <button type="button" className={tab === 'summary' ? 'active' : ''} onClick={() => setTab('summary')}>纪要</button>
        <button type="button" className={tab === 'transcript' ? 'active' : ''} onClick={() => setTab('transcript')}>转录</button>
      </div>
      {tab === 'summary' && (
        <SummaryViewer summaries={summary} templates={templates} onRetry={generateSummary} onToast={onToast} />
      )}
      {tab === 'transcript' && <TranscriptViewer transcript={transcript} onToast={onToast} />}
    </>
  );
}
