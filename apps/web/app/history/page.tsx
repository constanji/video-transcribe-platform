'use client';

import { useEffect, useMemo, useState } from 'react';
import HistoryDetailPanel from '../../components/HistoryDetailPanel';
import HistoryTimeline from '../../components/HistoryTimeline';
import Toast from '../../components/Toast';
import TopBar from '../../components/TopBar';
import { api } from '../../lib/api';
import type { Platform, Template, VideoAsset } from '../../lib/types';

export default function HistoryPage() {
  const [items, setItems] = useState<VideoAsset[]>([]);
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

  useEffect(() => {
    api<Platform[]>('/api/platforms').then(setPlatforms).catch(() => []);
    api<Template[]>('/api/templates').then(setTemplates).catch(() => []);
    load().catch(() => setItems([]));
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
          <h1>历史解析任务</h1>
          <p>按时间查看同一个视频资产下的下载、转录和总结版本。</p>
        </div>
      </div>
      <div className="history-filters">
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
      </div>
      <Toast message={toast} onClose={() => setToast('')} />
      <div className="history-layout">
        <section className="timeline">
          <HistoryTimeline items={filtered} platforms={platforms} selectedId={selected?.id} onSelect={select} />
        </section>
        <section className="history-detail">
          <HistoryDetailPanel
            video={selected}
            platforms={platforms}
            templates={templates}
            onRetry={async (jobId) => { await api(`/api/jobs/${jobId}/retry`, { method: 'POST' }); setToast('已重新排队'); await load(); }}
            onToast={(message) => setToast(message)}
            onDeleted={async (id) => { await api(`/api/videos/${id}`, { method: 'DELETE' }); setToast('已删除'); await load(); }}
          />
        </section>
      </div>
    </main>
  );
}
