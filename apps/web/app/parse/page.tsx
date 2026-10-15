'use client';

import { useEffect, useRef, useState } from 'react';
import LinkParserCard from '../../components/LinkParserCard';
import ParsedVideoCard from '../../components/ParsedVideoCard';
import PlatformLogoStrip from '../../components/PlatformLogoStrip';
import RecentVideoGrid from '../../components/RecentVideoGrid';
import Toast from '../../components/Toast';
import TopBar from '../../components/TopBar';
import { api } from '../../lib/api';
import { readWorkbenchDraft, writeWorkbenchDraft } from '../../lib/workbench-draft';
import type { MediaVariant, Platform, VideoAsset } from '../../lib/types';

type CookieGuide = {
  intro: string;
  steps: string[];
  note: string;
};

const COOKIE_TARGETS = [
  {
    id: 'shipinhao', keys: ['腾讯元宝', '视频号短链'], name: '微信视频号', site: 'https://yuanbao.tencent.com', host: 'yuanbao.tencent.com',
  },
  {
    id: 'pixiv', keys: ['Pixiv'], name: 'Pixiv', site: 'https://www.pixiv.net/', host: 'www.pixiv.net',
    guide: {
      intro: 'Pixiv 只需要 PHPSESSID 一项登录凭证，不用从请求头里翻整串 Cookie，用「应用程序」面板复制即可。',
      steps: [
        '点击下方打开 Pixiv，确认已经登录（未登录不会有 PHPSESSID）。',
        '按 `F12` 打开开发者工具，切到 **应用程序** 标签（Application；找不到就点标签栏右侧的 `»` 展开）。',
        '在左侧树展开 存储 → Cookie，点击 `https://www.pixiv.net`（注意选 www 这个，不是 imp.pixiv.net）。',
        '在表格里找到 `PHPSESSID` 一行，双击「值」单元格，按 `Ctrl+A` 全选后 `Ctrl+C` 复制。',
        '在下面粘贴，格式为 `PHPSESSID=复制的值`，然后保存。',
      ],
      note: '不要点「应用程序 → 存储」右侧的「清除所选内容」，会清掉 Pixiv 登录态。Pixiv 的 Cookie 不会主动轮换，手动复制一次可长期使用。',
    } satisfies CookieGuide,
  },
  {
    id: 'xhs', keys: ['小红书'], name: '小红书', site: 'https://www.xiaohongshu.com/', host: 'www.xiaohongshu.com',
  },
  {
    id: 'youtube', keys: ['YouTube'], name: 'YouTube', site: 'https://www.youtube.com/', host: 'www.youtube.com',
  },
  {
    id: 'qzone', keys: ['QQ 空间', 'QQ空间'], name: 'QQ空间', site: 'https://user.qzone.qq.com/', host: 'qzone.qq.com',
  },
  {
    id: 'xiaoheihe', keys: ['小黑盒'], name: '小黑盒', site: 'https://www.xiaoheihe.cn/', host: 'www.xiaoheihe.cn',
  },
  {
    id: 'tiktok', keys: ['TikTok'], name: 'TikTok', site: 'https://www.tiktok.com/', host: 'www.tiktok.com',
  },
];

function matchCookieTarget(message: string) {
  if (!message.includes('Cookie')) return null;
  return COOKIE_TARGETS.find((item) => item.keys.some((key) => message.includes(key))) || null;
}

export default function ParsePage() {
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
  const parseController = useRef<AbortController | null>(null);
  const parseRunning = useRef(false);
  const cookieTarget = matchCookieTarget(error);

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
    return () => parseController.current?.abort();
  }, []);

  useEffect(() => {
    if (!cookieGuideOpen) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setCookieGuideOpen(false);
    };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [cookieGuideOpen]);

  function changeUrl(value: string) {
    setUrl(value);
    writeWorkbenchDraft({ url: value });
  }

  async function parse(target = url) {
    if (parseRunning.current) return;
    const matched = target.trim().match(/https?:\/\/[^\s<>"'，。、]+/i);
    if (!matched) {
      setError('请输入包含 http:// 或 https:// 的有效视频链接');
      return;
    }
    const normalizedTarget = matched[0].replace(/[).,，。/\]]+$/, '');
    const controller = new AbortController();
    parseController.current = controller;
    parseRunning.current = true;
    setLoading(true);
    setError('');
    try {
      const parsed = await api<VideoAsset>('/api/parse', {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({ url: normalizedTarget }),
      });
      setVideo(parsed);
      setVariants(parsed.variants || await api<MediaVariant[]>(`/api/videos/${parsed.id}/variants`));
      writeWorkbenchDraft({ url: normalizedTarget, videoId: parsed.id });
      await loadRecent();
    } catch (err) {
      if (controller.signal.aborted) {
        setError('已取消本次解析');
        return;
      }
      const raw = (err as Error).message;
      const message = raw === 'Failed to fetch' ? '无法连接解析服务，请确认本机 API 正在运行后重试' : raw;
      setError(message);
      if (matchCookieTarget(message)) setCookieGuideOpen(true);
    } finally {
      if (parseController.current === controller) parseController.current = null;
      parseRunning.current = false;
      setLoading(false);
    }
  }

  function cancelParse() {
    parseController.current?.abort();
  }

  async function saveCookieAndRetry() {
    if (!cookie.trim() || !cookieTarget) return;
    setCookieSaving(true);
    try {
      const current = await api<any>('/api/settings/parser');
      await api('/api/settings/parser', {
        method: 'PUT',
        body: JSON.stringify({
          ...current,
          parsers: { ...(current.parsers || {}), [cookieTarget?.id || '']: { ...(current.parsers?.[cookieTarget?.id || ''] || {}), cookies: cookie.trim() } },
        }),
      });
      setCookieGuideOpen(false);
      setCookie('');
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
      <TopBar active="parse" />
      <section className="intro">
        <div className="eyebrow">VIDEO WORKSPACE</div>
        <h1>解析视频，提炼内容。</h1>
        <p>从一个链接开始，查看媒体信息后选择下载、转录或总结。</p>
      </section>
      <LinkParserCard url={url} loading={loading} onUrlChange={changeUrl} onParse={() => parse()} onCancel={cancelParse}>
        <PlatformLogoStrip platforms={platforms} />
      </LinkParserCard>
      {error && (
        <div className="alert error parser-alert">
          <div className="parser-alert-copy">
            <strong>{error}</strong>
            {cookieTarget && <small>需要登录态。本页和控制台都读不到对方的 Cookie，请按弹窗从请求头复制。</small>}
          </div>
          {cookieTarget && <button type="button" className="outline-button" onClick={() => setCookieGuideOpen(true)}>查看获取步骤</button>}
          <button type="button" onClick={() => setError('')} aria-label="关闭错误提示">×</button>
        </div>
      )}
      {cookieGuideOpen && cookieTarget && (
        <div className="cookie-guide-mask" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setCookieGuideOpen(false);
        }}>
          <section className="cookie-guide" role="dialog" aria-modal="true" aria-labelledby="cookie-guide-title">
            <button type="button" className="cookie-guide-close" onClick={() => setCookieGuideOpen(false)} aria-label="关闭引导">×</button>
            <span className="eyebrow">需要登录态</span>
            <h2 id="cookie-guide-title">获取{cookieTarget.name} Cookie</h2>
            {cookieTarget.guide ? (
              <>
                <p>{cookieTarget.guide.intro}</p>
                <ol>
                  {cookieTarget.guide.steps.map((step, index) => (
                    <li
                      key={index}
                      dangerouslySetInnerHTML={{
                        __html: step
                          .replace(/&/g, '&amp;').replace(/</g, '&lt;')
                          .replace(/`([^`]+)`/g, '<code>$1</code>')
                          .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>'),
                      }}
                    />
                  ))}
                </ol>
                <div className="cookie-guide-note">{cookieTarget.guide.note}</div>
              </>
            ) : (
              <>
                <p>这个页面拿不到 {cookieTarget.name} 的登录状态。登录票据通常是 HttpOnly，在对方页面的控制台执行 document.cookie 也读不到。不安装浏览器扩展的话，只能从开发者工具的请求头里复制一次。</p>
                <ol>
                  <li>点击下方打开 {cookieTarget.name}，确认已经登录。</li>
                  <li>按 <kbd>F12</kbd> 打开开发者工具，切到 <strong>Network / 网络</strong>，勾选 <strong>Preserve log / 保留日志</strong>。</li>
                  <li>刷新那个页面，点开任意发往 <code>{cookieTarget.host}</code> 的请求。</li>
                  <li>在 <strong>Request Headers / 请求标头</strong> 里找到 <strong>Cookie</strong>，只复制冒号后面的内容。</li>
                  <li>粘贴到下面并保存。Cookie 只写进本机解析器设置，不会回显原文。</li>
                </ol>
                <div className="cookie-guide-note">找不到 Cookie 时：右键请求 → Copy → Copy as cURL，再从 <code>--cookie</code> 或 <code>-H &apos;cookie: ...&apos;</code> 里取出值。</div>
              </>
            )}
            <div className="cookie-guide-field">
              <input autoFocus value={cookie} onChange={(event) => setCookie(event.target.value)} placeholder="粘贴 Cookie" aria-label={`${cookieTarget.name} Cookie`} />
              <button type="button" className="primary-button" onClick={saveCookieAndRetry} disabled={cookieSaving || !cookie.trim()}>{cookieSaving ? '保存中…' : '保存并重试'}</button>
            </div>
            <a className="cookie-guide-link" href={cookieTarget.site} target="_blank" rel="noreferrer">打开{cookieTarget.name} ↗</a>
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
          loading={loading}
          initialPanel={actionPanel}
          onReparse={() => {
            if (!loading) void parse(video?.source_url || url);
          }}
          onToast={(message, kind) => setToast({ message, kind })}
          onPanelChange={(panel) => {
            setActionPanel(panel);
            writeWorkbenchDraft({ panel });
          }}
          onClose={() => {
            parseController.current?.abort();
            setLoading(false);
            setVideo(null);
            setVariants([]);
            setActionPanel(null);
            writeWorkbenchDraft({ videoId: '', panel: null });
            document.querySelector('.intro')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
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
