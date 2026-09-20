'use client';

import { useEffect, useState } from 'react';
import HistoryDetailPanel from '../../../components/HistoryDetailPanel';
import Toast from '../../../components/Toast';
import TopBar from '../../../components/TopBar';
import { api } from '../../../lib/api';
import type { Platform, Template, VideoAsset } from '../../../lib/types';

export default function VideoDetailPage({ params }: { params: { id: string } }) {
  const [video, setVideo] = useState<VideoAsset>();
  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [toast, setToast] = useState('');

  useEffect(() => {
    api<VideoAsset>(`/api/history/${params.id}`).then(setVideo).catch(() => undefined);
    api<Platform[]>('/api/platforms').then(setPlatforms);
    api<Template[]>('/api/templates').then(setTemplates);
  }, [params.id]);

  return (
    <main className="page-shell">
      <TopBar active="history" />
      <div className="page-heading">
        <div>
          <span className="eyebrow">VIDEO</span>
          <h1>视频详情</h1>
          <p><a href="/history">返回历史解析任务</a></p>
        </div>
      </div>
      <Toast message={toast} onClose={() => setToast('')} />
      <section className="history-detail">
        <HistoryDetailPanel
          video={video}
          platforms={platforms}
          templates={templates}
          onRetry={async (jobId) => { await api(`/api/jobs/${jobId}/retry`, { method: 'POST' }); setToast('已重新排队'); }}
          onToast={setToast}
          onDeleted={async (id) => { await api(`/api/videos/${id}`, { method: 'DELETE' }); window.location.href = '/history'; }}
        />
      </section>
    </main>
  );
}
