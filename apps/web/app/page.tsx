'use client';

import { useEffect, useState } from 'react';
import MeetingWorkbench from '../components/MeetingWorkbench';
import RecentMeetingGrid from '../components/RecentMeetingGrid';
import Toast from '../components/Toast';
import TopBar from '../components/TopBar';
import { api } from '../lib/api';
import { listMeetings } from '../lib/meeting-api';
import type { MeetingHistoryItem, Platform } from '../lib/types';

const MEETINGS_POLL_MS = 15000;

export default function MeetingHomePage() {
  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [meetings, setMeetings] = useState<MeetingHistoryItem[]>([]);
  const [toast, setToast] = useState<{ message: string; kind?: 'success' | 'error' }>({ message: '' });

  useEffect(() => {
    api<Platform[]>('/api/platforms').then(setPlatforms).catch(() => setPlatforms([]));
  }, []);

  // 会议在录制/转写过程中状态持续变化：轻量轮询让底部历史列表保持新鲜，
  // 不侵入 MeetingWorkbench 内部的录制状态机。
  useEffect(() => {
    let cancelled = false;
    async function load() {
      const data = await listMeetings().catch(() => [] as MeetingHistoryItem[]);
      if (!cancelled) setMeetings(data);
    }
    load();
    const timer = window.setInterval(load, MEETINGS_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  return (
    <main className="page-shell upload-page">
      <TopBar active="meeting" />
      <div className="page-heading">
        <div>
          <span className="eyebrow">MEETING NOTES</span>
          <h1>会议纪要</h1>
          <p>点击开始录音。实时转录会边说边出字；录音转录在停止后整段转写。完成后可选择模板生成纪要。</p>
        </div>
      </div>
      <Toast message={toast.message} kind={toast.kind} onClose={() => setToast({ message: '' })} />
      <MeetingWorkbench platforms={platforms} onToast={(message, kind) => setToast({ message, kind })} />
      <RecentMeetingGrid meetings={meetings} />
    </main>
  );
}
