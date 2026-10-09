'use client';

import { useEffect, useMemo, useState } from 'react';
import HistoryDetailPanel from '../../components/HistoryDetailPanel';
import HistoryTimeline from '../../components/HistoryTimeline';
import MeetingHistoryDetail from '../../components/MeetingHistoryDetail';
import MeetingHistoryList from '../../components/MeetingHistoryList';
import Toast from '../../components/Toast';
import TopBar from '../../components/TopBar';
import { listMeetings } from '../../lib/meeting-api';
import { api } from '../../lib/api';
import type { MeetingHistoryItem, Platform, Template, VideoAsset } from '../../lib/types';

export default function HistoryPage() {
  const [tab, setTab] = useState<'video' | 'meeting'>(() => (new URLSearchParams(window.location.search).get('tab') === 'meeting' ? 'meeting' : 'video'));
  const [items, setItems] = useState<VideoAsset[]>([]);
  const [meetings, setMeetings] = useState<MeetingHistoryItem[]>([]);
  const [selectedMeeting, setSelectedMeeting] = useState<MeetingHistoryItem>();
  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [selected, setSelected] = useState<VideoAsset>();
  const [q, setQ] = useState('');
  const [platform, setPlatform] = useState('');
  const [jobType, setJobType] = useState('');
  const [status, setStatus] = useState('');
  const [view, setView] = useState<'all' | 'video' | 'summary'>('all');
  const [toast, setToast] = useState('');

  async function load() {
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (platform) params.set('platform', platform);
    if (jobType) params.set('job_type', jobType);
    if (status) params.set('status', status);
    const data = await api<VideoAsset[]>(`/api/history?${params.toString()}`);
    setItems(data);
    const queryId = new URLSearchParams(window.location.search).get('video');
    const next = data.find((item) => item.id === (queryId || selected?.id)) || data[0];
    if (next) {
      const detail = await api<VideoAsset>(`/api/history/${next.id}`).catch(() => next);
      setSelected(detail);
    } else {
      setSelected(undefined);
    }
  }

  async function loadMeetings() {
    const data = await listMeetings().catch(() => []);
    setMeetings(data);
    if (data.length > 0) {
      // 支持 /history?tab=meeting&meeting=<id> 深链：主页会议卡片点击后直达该会议详情。
      const queryMeeting = new URLSearchParams(window.location.search).get('meeting');
      setSelectedMeeting((prev) => {
        const wanted = data.find((item) => item.id === (queryMeeting || prev?.id));
        return wanted || data[0];
      });
    } else {
      setSelectedMeeting(undefined);
    }
  }

  useEffect(() => {
    api<Platform[]>('/api/platforms').then(setPlatforms).catch(() => []);
    api<Template[]>('/api/templates').then(setTemplates).catch(() => []);
    load().catch(() => setItems([]));
    loadMeetings();
  }, []);

  const filtered = useMemo(() => {
    return items.filter((item) => {
      if (view === 'summary' && !item.has_summary) return false;
      if (view === 'video' && item.has_summary) return false;
      return true;
    });
  }, [items, view]);

  async function select(item: VideoAsset) {
    const detail = await api<VideoAsset>(`/api/history/${item.id}`);
    setSelected(detail);
  }

  return (
    <main className="page-shell">
      <TopBar active="history" />
      <div className="page-heading">
        <div>
          <span className="eyebrow">HISTORY</span>
          <h1>历史任务</h1>
          <p>按时间查看视频解析与会议纪要任务。</p>
        </div>
      </div>
      <div className="history-filters">
        <div className="view-switch">
          {(['video', 'meeting'] as const).map((id) => (
            <button type="button" key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>
              {id === 'video' ? '视频解析' : '会议纪要'}
            </button>
          ))}
        </div>
        {tab === 'video' && (
          <>
            <input className="search-input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索标题、作者或平台" />
            <select value={platform} onChange={(e) => setPlatform(e.target.value)}>
              <option value="">全部平台</option>
              {platforms.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
            <select value={jobType} onChange={(e) => setJobType(e.target.value)}>
              <option value="">全部任务类型</option>
              <option value="download">下载</option>
              <option value="transcribe">转录</option>
              <option value="summarize">总结</option>
            </select>
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">全部状态</option>
              <option value="queued">排队</option>
              <option value="running">进行中</option>
              <option value="completed">完成</option>
              <option value="failed">失败</option>
            </select>
            <div className="view-switch">
              {(['all', 'video', 'summary'] as const).map((id) => (
                <button type="button" key={id} className={view === id ? 'active' : ''} onClick={() => setView(id)}>
                  {id === 'all' ? '全部' : id === 'video' ? '视频' : '总结'}
                </button>
              ))}
            </div>
            <button type="button" className="outline-button" onClick={() => load()}>筛选</button>
          </>
        )}
      </div>
      <Toast message={toast} onClose={() => setToast('')} />
      <div className="history-layout">
        <section className="timeline">
          {tab === 'video' ? (
            <HistoryTimeline items={filtered} platforms={platforms} selectedId={selected?.id} onSelect={select} />
          ) : (
            <MeetingHistoryList
              items={meetings}
              selectedId={selectedMeeting?.id}
              onSelect={(item) => setSelectedMeeting(item)}
            />
          )}
        </section>
        <section className="history-detail">
          {tab === 'video' ? (
            <HistoryDetailPanel
              video={selected}
              platforms={platforms}
              templates={templates}
              onRetry={async (jobId) => { await api(`/api/jobs/${jobId}/retry`, { method: 'POST' }); setToast('已重新排队'); await load(); }}
              onToast={(message) => setToast(message)}
              onDeleted={async (id) => { await api(`/api/videos/${id}`, { method: 'DELETE' }); setToast('已删除'); await load(); }}
            />
          ) : (
            <MeetingHistoryDetail
              meetingId={selectedMeeting?.id}
              templates={templates}
              onToast={(message) => setToast(message)}
            />
          )}
        </section>
      </div>
    </main>
  );
}
