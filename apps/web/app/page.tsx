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
  const [cookie, setCookie] = useState('');
  const [cookieGuideOpen, setCookieGuideOpen] = useState(false);
  const [cookieSaving, setCookieSaving] = useState(false);
  const needsShipinhaoCookie = error.includes('腾讯元宝 Cookie') || error.includes('视频号短链需要配置');

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
      const message = (err as Error).message;
      setError(message);
      if (message.includes('腾讯元宝 Cookie') || message.includes('视频号短链需要配置')) {
        setCookieGuideOpen(true);
      }
    } finally {
      setLoading(false);
    }
  }

  async function saveCookieAndRetry() {
    if (!cookie.trim()) return;
    setCookieSaving(true);
    try {
      const current = await api<any>('/api/settings/parser');
      await api('/api/settings/parser', {
        method: 'PUT',
        body: JSON.stringify({
          ...current,
          parsers: { ...(current.parsers || {}), shipinhao: { ...(current.parsers?.shipinhao || {}), cookies: cookie.trim() } },
        }),
      });
      setCookieGuideOpen(false);
      await parse(url);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setCookieSaving(false);
    }
  }

  async function focusVideo(item: VideoAsset, panel?: 'download' | 'summary' | 'transcribe' | 'summarize') {
    const sourceUrl = item.parse_status === 'uploaded' ? '' : item.source_url;
    setUrl(sourceUrl);
    setActionPanel(panel || null);
    writeWorkbenchDraft({ url: sourceUrl, videoId: item.id, panel: panel || null });
    try {
      const stored = await api<VideoAsset>(`/api/videos/${item.id}`);
      setVideo(stored);
      setVariants(stored.variants || []);
      window.setTimeout(() => document.getElementById('workbench-result')?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 0);
    } catch {
      setVideo(item);
      setVariants(await api<MediaVariant[]>(`/api/videos/${item.id}/variants`).catch(() => []));
      window.setTimeout(() => document.getElementById('workbench-result')?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 0);
    }
  }

  async function deleteRecent(item: VideoAsset) {
    if (!window.confirm(`确定删除“${item.title}”吗？`)) return;
    try {
      await api(`/api/videos/${item.id}`, { method: 'DELETE' });
      if (item.id === video?.id) {
        setVideo(null);
        setVariants([]);
        setActionPanel(null);
        writeWorkbenchDraft({ videoId: '', panel: null });
      }
      await loadRecent();
      setToast({ message: '已删除解析记录', kind: 'success' });
    } catch (err) {
      setToast({ message: (err as Error).message, kind: 'error' });
    }
  }

  return (
    <main className="app-shell">
      <TopBar active="workbench" />
      <section className="intro">
        <div className="eyebrow">VIDEO WORKSPACE</div>
        <h1>解析视频，提炼内容。</h1>
        <p>从一个链接开始，查看媒体信息后选择下载、转录或总结。</p>
      </section>
      <LinkParserCard url={url} loading={loading} onUrlChange={changeUrl} onParse={() => parse()}>
        <PlatformLogoStrip platforms={platforms} />
      </LinkParserCard>
      {error && (
        <div className="alert error parser-alert">
          <div className="parser-alert-copy"><strong>{error}</strong>{needsShipinhaoCookie && <small>按下方步骤获取 Cookie，粘贴后即可继续解析当前链接。</small>}</div>
          {needsShipinhaoCookie && (
            <div className="cookie-inline-form">
              <input value={cookie} onChange={(event) => setCookie(event.target.value)} placeholder="粘贴腾讯元宝 Cookie" aria-label="腾讯元宝 Cookie" />
              <button type="button" className="outline-button" onClick={saveCookieAndRetry} disabled={cookieSaving || !cookie.trim()}>{cookieSaving ? '保存中…' : '保存并重试'}</button>
              <a href="https://yuanbao.tencent.com" target="_blank" rel="noreferrer">打开腾讯元宝 ↗</a>
            </div>
          )}
          <button type="button" onClick={() => setError('')}>×</button>
        </div>
      )}
      {cookieGuideOpen && (
        <div className="cookie-guide-mask" role="dialog" aria-modal="true" aria-labelledby="cookie-guide-title">
          <section className="cookie-guide">
            <button type="button" className="cookie-guide-close" onClick={() => setCookieGuideOpen(false)} aria-label="关闭引导">×</button>
            <span className="eyebrow">首次配置</span>
            <h2 id="cookie-guide-title">获取腾讯元宝 Cookie</h2>
            <p>视频号短链需要借助腾讯元宝获取临时视频地址，只需首次配置一次。当前浏览器无法让本页面直接读取元宝的跨域 Cookie，因此需要你从请求标头复制一次。</p>
            <ol>
              <li>点击下方“打开腾讯元宝”，登录并打开任意对话页，保持该标签页不要关闭。</li>
              <li>按 <kbd>F12</kbd>（Mac：<kbd>⌥⌘I</kbd>）打开开发者工具，切到 <strong>Network / 网络</strong>，勾选 <strong>Preserve log / 保留日志</strong>。</li>
              <li>刷新元宝页面，或发送一条消息，让请求列表重新出现。</li>
              <li>点开任意发往 <code>yuanbao.tencent.com</code> 的请求，在右侧找到 <strong>Headers / 标头</strong> → <strong>Request Headers / 请求标头</strong> → <strong>Cookie</strong>。</li>
              <li>只复制 <code>Cookie:</code> 后面的整行内容，不要复制 <code>Cookie:</code> 这个字段名，也不要复制 Response Headers。</li>
              <li>回到这里粘贴到输入框，点击“保存并重试”。Cookie 仅保存到当前服务端配置，不会显示原文。</li>
            </ol>
            <div className="cookie-guide-note">找不到 Cookie 时：右键请求 → Copy → Copy as cURL，然后从命令中的 <code>--cookie</code> 或 <code>-H 'cookie: ...'</code> 复制值。</div>
            <div className="cookie-guide-field"><input value={cookie} onChange={(event) => setCookie(event.target.value)} placeholder="粘贴 Cookie" aria-label="腾讯元宝 Cookie" /><button type="button" className="primary-button" onClick={saveCookieAndRetry} disabled={cookieSaving || !cookie.trim()}>{cookieSaving ? '保存中…' : '保存并重试'}</button></div>
            <a className="cookie-guide-link" href="https://yuanbao.tencent.com" target="_blank" rel="noreferrer">打开腾讯元宝 ↗</a>
          </section>
        </div>
      )}
      <Toast message={toast.message} kind={toast.kind} onClose={() => setToast({ message: '' })} />
      {(loading || video) && (
        <ParsedVideoCard
          id="workbench-result"
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
        onOpen={(item) => focusVideo(item, 'transcribe')}
        onDelete={deleteRecent}
      />
    </main>
  );
}
