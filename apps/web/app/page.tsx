'use client';

import { useEffect, useState } from 'react';
import MeetingWorkbench from '../components/MeetingWorkbench';
import RecentMeetingGrid from '../components/RecentMeetingGrid';
import Toast from '../components/Toast';
import TopBar from '../components/TopBar';
import TranscriptStudio from '../components/TranscriptStudio';
import { api } from '../lib/api';
import { deleteMeeting, listMeetings } from '../lib/meeting-api';
import type { MeetingHistoryItem, Platform, SummarySettings, Template } from '../lib/types';

const MEETINGS_POLL_MS = 15000;

export default function MeetingHomePage() {
  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [meetings, setMeetings] = useState<MeetingHistoryItem[]>([]);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [templateId, setTemplateId] = useState('');
  const [summaryReady, setSummaryReady] = useState(true);
  const [studioVideoId, setStudioVideoId] = useState<string | null>(null);
  const [studioMeetingId, setStudioMeetingId] = useState<string | null>(null);
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

  // 结果面板需要模板列表与默认模板：会议场景优先「标准会议纪要」，
  // 缺失时退到通用模板，与工作台完成后的总结入口保持同一默认。
  useEffect(() => {
    api<Template[]>('/api/templates?listed=true').then((items) => {
      setTemplates(items);
      // 管理员标记的默认模板优先；没有时退回内置命名约定。
      const preferred = items.find((item) => item.is_default)
        || items.find((item) => item.name === '标准会议纪要')
        || items.find((item) => item.name === '通用视频总结')
        || items[0];
      setTemplateId(preferred?.id || '');
    }).catch(() => setTemplates([]));
    // 只为结果面板的模型选择器提供初始占位；配置读取完成后以面板内读取的为准。
    api<SummarySettings>('/api/settings/transcription').then((settings) => {
      setSummaryReady(settings.default_provider === 'local'
        ? Boolean(settings.summary_model_found)
        : Boolean(settings.api_base_url));
    }).catch(() => setSummaryReady(false));
  }, []);

  // 轮询刷新会整体覆盖 meetings，删除走乐观更新：先移除再调接口，失败提示并恢复。
  async function removeMeeting(item: MeetingHistoryItem) {
    if (!window.confirm(`确定删除「${item.title}」吗？会议录音、转录和纪要会一并删除。`)) return;
    const previous = meetings;
    setMeetings((current) => current.filter((entry) => entry.id !== item.id));
    if (studioMeetingId === item.id) {
      setStudioVideoId(null);
      setStudioMeetingId(null);
    }
    try {
      await deleteMeeting(item.id);
      setToast({ message: '已删除会议记录', kind: 'success' });
    } catch (err) {
      setMeetings(previous);
      setToast({ message: (err as Error).message, kind: 'error' });
    }
  }

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
      <RecentMeetingGrid
        meetings={meetings}
        onOpen={(item) => {
          setStudioVideoId(item.video_id || null);
          setStudioMeetingId(item.id);
        }}
        onDelete={removeMeeting}
      />
      {studioVideoId && (
        <TranscriptStudio
          videoId={studioVideoId}
          meetingId={studioMeetingId || undefined}
          templateId={templateId}
          templateName={templates.find((item) => item.id === templateId)?.name}
          templates={templates}
          onTemplateChange={setTemplateId}
          summaryReady={summaryReady}
          onClose={() => {
            setStudioVideoId(null);
            setStudioMeetingId(null);
          }}
          onToast={(message, kind) => setToast({ message, kind })}
        />
      )}
    </main>
  );
}
