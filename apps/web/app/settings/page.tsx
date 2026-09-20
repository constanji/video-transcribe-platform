'use client';

import { useEffect, useMemo, useState } from 'react';
import ParserSettings, { PARSER_DEFAULTS } from '../../components/ParserSettings';
import ServiceSettings from '../../components/ServiceSettings';
import SettingsTabs from '../../components/SettingsTabs';
import TemplateSettings from '../../components/TemplateSettings';
import Toast from '../../components/Toast';
import TopBar from '../../components/TopBar';
import TranscriptionSettings from '../../components/TranscriptionSettings';
import { api } from '../../lib/api';
import type { Template } from '../../lib/types';

type Tab = 'parser' | 'transcription' | 'templates' | 'services';

export default function SettingsPage() {
  const [tab, setTab] = useState<Tab>('parser');
  const [parser, setParser] = useState(PARSER_DEFAULTS);
  const [transcription, setTranscription] = useState<any>({});
  const [services, setServices] = useState<any>({});
  const [saved, setSaved] = useState<Record<Exclude<Tab, 'templates'>, any>>({ parser: PARSER_DEFAULTS, transcription: {}, services: {} });
  const [templates, setTemplates] = useState<Template[]>([]);
  const [toast, setToast] = useState<{ message: string; kind?: 'success' | 'error' }>({ message: '' });

  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get('tab');
    if (requested === 'parser' || requested === 'transcription' || requested === 'templates' || requested === 'services') {
      setTab(requested);
    }
  }, []);

  useEffect(() => {
    Promise.all([
      api<any>('/api/settings/parser'),
      api<any>('/api/settings/transcription'),
      api<any>('/api/settings/services'),
      api<Template[]>('/api/templates'),
    ]).then(([p, t, s, tm]) => {
      setParser({ ...PARSER_DEFAULTS, ...p });
      setTranscription(t);
      setServices(s);
      setSaved({ parser: { ...PARSER_DEFAULTS, ...p }, transcription: t, services: s });
      setTemplates(tm);
    }).catch((err) => setToast({ message: err.message, kind: 'error' }));
  }, []);

  const current = tab === 'parser' ? parser : tab === 'transcription' ? transcription : tab === 'services' ? services : null;
  const dirty = useMemo(() => {
    if (tab === 'templates') return false;
    return JSON.stringify(current) !== JSON.stringify(saved[tab]);
  }, [current, saved, tab]);

  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (dirty) event.preventDefault();
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [dirty]);

  function changeTab(next: Tab) {
    if (dirty && !window.confirm('当前 Tab 有未保存的更改，确定离开？')) return;
    setTab(next);
  }

  async function save() {
    if (tab === 'templates') return;
    try {
      const payload = tab === 'parser' ? parser : tab === 'transcription' ? transcription : services;
      const stored = await api<any>(`/api/settings/${tab}`, { method: 'PUT', body: JSON.stringify(payload) });
      setSaved({ ...saved, [tab]: stored });
      if (tab === 'parser') setParser({ ...PARSER_DEFAULTS, ...stored });
      if (tab === 'transcription') setTranscription(stored);
      if (tab === 'services') setServices(stored);
      setToast({ message: '设置已保存' });
    } catch (err) {
      setToast({ message: (err as Error).message, kind: 'error' });
    }
  }

  function restore() {
    if (tab === 'parser') setParser(PARSER_DEFAULTS);
    if (tab === 'transcription') setTranscription({});
    if (tab === 'services') setServices({});
  }

  return (
    <main className="page-shell">
      <TopBar active="settings" />
      <div className="page-heading">
        <div>
          <span className="eyebrow">SETTINGS</span>
          <h1>设置</h1>
          <p>解析器、转录、模板和服务连接按 Tab 分别保存到服务端。</p>
        </div>
        {dirty && <span className="saved-note">有未保存更改</span>}
      </div>
      <Toast message={toast.message} kind={toast.kind} onClose={() => setToast({ message: '' })} />
      <div className="settings-card">
        <SettingsTabs value={tab} onChange={changeTab} />
        <div className="settings-content">
          {tab === 'parser' && <ParserSettings value={parser} onChange={setParser} />}
          {tab === 'transcription' && (
            <TranscriptionSettings
              value={transcription}
              onChange={setTranscription}
              onToast={(message, kind) => setToast({ message, kind })}
            />
          )}
          {tab === 'templates' && (
            <TemplateSettings
              templates={templates}
              onTemplates={setTemplates}
              onToast={(message, kind) => setToast({ message, kind })}
            />
          )}
          {tab === 'services' && <ServiceSettings value={services} onChange={setServices} />}
          {tab !== 'templates' && (
            <div className="detail-actions">
              <button type="button" className="save-settings" onClick={save}>保存当前分组</button>
              <button type="button" className="outline-button" onClick={restore}>恢复默认值</button>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
