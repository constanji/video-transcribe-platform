'use client';

import { useEffect, useState } from 'react';
import LinkParserCard from '../components/LinkParserCard';
import ParsedVideoCard from '../components/ParsedVideoCard';
import PlatformLogoStrip from '../components/PlatformLogoStrip';
import RecentVideoGrid from '../components/RecentVideoGrid';
import Toast from '../components/Toast';
import TopBar from '../components/TopBar';
import { api } from '../lib/api';
import { readWorkbenchDraft, writeWorkbenchDraft } from '../lib/workbench-draft';
import type { MediaVariant, Platform, VideoAsset } from '../lib/types';

export default function Home() {
  const [url, setUrl] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [toast, setToast] = useState<{ message: string; kind?: 'success' | 'error' }>({ message: '' });
  const [video, setVideo] = useState<VideoAsset | null>(null);
  const [variants, setVariants] = useState<MediaVariant[]>([]);
  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [recent, setRecent] = useState<VideoAsset[]>([]);
  const [actionPanel, setActionPanel] = useState<'download' | 'summary' | 'transcribe' | 'summarize' | null>(null);

  async function loadRecent() {
    setRecent(await api<VideoAsset[]>('/api/videos/recent').catch(() => []));
  }

  async function restoreVideo(videoId: string, panel?: 'download' | 'summary' | 'transcribe' | 'summarize' | null) {
    try {
      const stored = await api<VideoAsset>(`/api/videos/${videoId}`);
      setVideo(stored);
      setVariants(stored.variants || await api<MediaVariant[]>(`/api/videos/${stored.id}/variants`).catch(() => []));
      if (panel) setActionPanel(panel);
    } catch {
      writeWorkbenchDraft({ videoId: '' });
    }
  }

  useEffect(() => {
    const draft = readWorkbenchDraft();
    setUrl(draft.url);
    setActionPanel(draft.panel || null);
    api<Platform[]>('/api/platforms').then(setPlatforms).catch(() => setPlatforms([]));
    loadRecent();
    if (draft.videoId) {
      void restoreVideo(draft.videoId, draft.panel);
    }
  }, []);

  function changeUrl(value: string) {
    setUrl(value);
    writeWorkbenchDraft({ url: value });
  }

  async function parse(target = url) {
    if (!target.trim()) return;
    setLoading(true);
    setError('');
    try {
      const parsed = await api<VideoAsset>('/api/parse', {
        method: 'POST',
        body: JSON.stringify({ url: target.trim() }),
      });
      setVideo(parsed);
      setVariants(parsed.variants || await api<MediaVariant[]>(`/api/videos/${parsed.id}/variants`));
      writeWorkbenchDraft({ url: target.trim(), videoId: parsed.id });
      await loadRecent();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  async function focusVideo(item: VideoAsset, panel?: 'download' | 'summary' | 'transcribe' | 'summarize') {
    setUrl(item.source_url);
    setActionPanel(panel || null);
    writeWorkbenchDraft({ url: item.source_url, videoId: item.id, panel: panel || null });
    window.scrollTo({ top: 0, behavior: 'smooth' });
    try {
      const stored = await api<VideoAsset>(`/api/videos/${item.id}`);
      setVideo(stored);
      setVariants(stored.variants || []);
    } catch {
      setVideo(item);
      setVariants(await api<MediaVariant[]>(`/api/videos/${item.id}/variants`).catch(() => []));
    }
  }

  return (
    <main className="app-shell">
      <TopBar active="workbench" />
      <section className="intro">
        <div className="eyebrow">VIDEO WORKSPACE</div>
        <h1>解析视频，提炼内容。</h1>
        <p>从一个链接开始，查看媒体信息后选择下载或总结。</p>
      </section>
      <LinkParserCard url={url} loading={loading} onUrlChange={changeUrl} onParse={() => parse()}>
        <PlatformLogoStrip platforms={platforms} />
      </LinkParserCard>
      {error && <div className="alert error">{error}<button type="button" onClick={() => setError('')}>×</button></div>}
      <Toast message={toast.message} kind={toast.kind} onClose={() => setToast({ message: '' })} />
      {(loading || video) && (
        <ParsedVideoCard
          key={`${video?.id || 'pending'}-${video?.last_processed_at || 'new'}`}
          video={video || { id: '', source_url: url, title: '', parse_status: 'parsing', created_at: '' }}
          platforms={platforms}
          variants={variants}
          loading={loading && !video}
          initialPanel={actionPanel}
          onReparse={() => parse(video?.source_url || url)}
          onToast={(message, kind) => setToast({ message, kind })}
          onPanelChange={(panel) => {
            setActionPanel(panel);
            writeWorkbenchDraft({ panel });
          }}
        />
      )}
      <RecentVideoGrid
        videos={recent}
        platforms={platforms}
        onDownload={(item) => focusVideo(item, 'download')}
        onSummary={(item) => focusVideo(item, 'transcribe')}
      />
    </main>
  );
}
