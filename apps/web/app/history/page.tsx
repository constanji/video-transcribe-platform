'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import HistoryDetailPanel from '../../components/HistoryDetailPanel';
import HistoryTimeline from '../../components/HistoryTimeline';
import MeetingHistoryDetail from '../../components/MeetingHistoryDetail';
import MeetingHistoryList from '../../components/MeetingHistoryList';
import Toast from '../../components/Toast';
import TopBar from '../../components/TopBar';
import { listMeetings } from '../../lib/meeting-api';
import { api } from '../../lib/api';
import { isImageRecord } from '../../lib/media-kind';
import type { MeetingHistoryItem, Platform, Template, VideoAsset } from '../../lib/types';

export default function HistoryPage() {
  // 初始值必须 SSR 安全（window 在服务端预渲染时不存在）；URL 参数在挂载后读取。
  const [tab, setTab] = useState<'video' | 'meeting'>('video');
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
  const [media, setMedia] = useState<'all' | 'video' | 'image'>('all');
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
    // ?tab=meeting 深链：hydration 完成后再切 tab，避免 SSR/客户端首帧不一致。
    const params = new URLSearchParams(window.location.search);
    if (params.get('tab') === 'meeting') setTab('meeting');
    if (params.get('media') === 'video' || params.get('media') === 'image') setMedia(params.get('media') as 'video' | 'image');
    api<Platform[]>('/api/platforms').then(setPlatforms).catch(() => []);
    api<Template[]>('/api/templates').then(setTemplates).catch(() => []);
    load().catch(() => setItems([]));
    loadMeetings();
  }, []);

  useEffect(() => {
    if (tab !== 'meeting') return;
    const timer = window.setInterval(() => void loadMeetings(), 15000);
    return () => window.clearInterval(timer);
  }, [tab]);

  const selectedJobRunning = Boolean(selected?.jobs?.some((job) => !['completed', 'failed', 'cancelled'].includes(job.status)));
  useEffect(() => {
    if (tab !== 'video' || !selected?.id || !selectedJobRunning) return;
    const refresh = async () => {
      try {
        const detail = await api<VideoAsset>(`/api/history/${selected.id}`);
        setSelected(detail);
        setItems((current) => current.map((item) => item.id === detail.id ? { ...item, ...detail } : item));
      } catch {
        // 短暂断网时保留当前详情，下个轮询周期继续。
      }
    };
    const timer = window.setInterval(refresh, 3000);
    return () => window.clearInterval(timer);
  }, [tab, selected?.id, selectedJobRunning]);

  const filtered = useMemo(() => {
    return items.filter((item) => {
      if (view === 'summary' && !item.has_summary) return false;
      if (view === 'video' && item.has_summary) return false;
      if (media === 'image' && !isImageRecord(item)) return false;
      if (media === 'video' && isImageRecord(item)) return false;
      return true;
    });
  }, [items, view, media]);

  const mediaRef = useRef(media);
  useEffect(() => {
    if (tab !== 'video' || !items.length) return;
    const changed = mediaRef.current !== media;
    mediaRef.current = media;
    const queryId = new URLSearchParams(window.location.search).get('video');
    const linked = queryId ? filtered.find((item) => item.id === queryId) : undefined;
    if (linked && selected?.id !== linked.id) {
      void select(linked);
      return;
    }
    if (selected && filtered.some((item) => item.id === selected.id)) return;
    // 首次加载仍由 load() 按 URL 选中；只有切换类型后才改选中项。
    if (!changed) return;
    if (!filtered[0]) {
      setSelected(undefined);
      return;
    }
    void select(filtered[0]);
  }, [media, filtered, selected, tab, items.length]);

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
            <div className="media-switch" role="tablist" aria-label="解析记录类型">
              {([['all', '全部'], ['video', '视频'], ['image', '图片']] as const).map(([id, label]) => (
                <button type="button" key={id} className={media === id ? 'active' : ''} onClick={() => setMedia(id)}>{label}</button>
              ))}
            </div>
            <div className="view-switch">
              {(['all', 'video', 'summary'] as const).map((id) => (
                <button type="button" key={id} className={view === id ? 'active' : ''} onClick={() => setView(id)}>
                  {id === 'all' ? '全部进度' : id === 'video' ? '未总结' : '已总结'}
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
            <HistoryTimeline items={filtered} platforms={platforms} selectedId={selected?.id} onSelect={select} emptyText={media === 'image' ? '还没有图片解析记录' : media === 'video' ? '还没有视频解析记录' : undefined} />
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
              onRefresh={() => void load()}
              onRetry={async (jobId) => {
                try {
                  await api(`/api/jobs/${jobId}/retry`, { method: 'POST' });
                  setToast('已重新排队');
                  await load();
                } catch (error) {
                  setToast((error as Error).message);
                }
              }}
              onToast={(message) => setToast(message)}
              onDeleted={async (id) => {
                try {
                  await api(`/api/videos/${id}`, { method: 'DELETE' });
                  setToast('已删除');
                  await load();
                } catch (error) {
                  setToast((error as Error).message);
                }
              }}
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
