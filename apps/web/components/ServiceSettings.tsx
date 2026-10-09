'use client';

import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import type { ApiKeyRecord } from '../lib/types';

export default function ServiceSettings({ value, onChange }: { value: any; onChange: (value: any) => void }) {
  const data = value || {};
  const [keys, setKeys] = useState<ApiKeyRecord[]>([]);
  const [keyName, setKeyName] = useState('');
  const [created, setCreated] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  function patch(partial: Record<string, unknown>) {
    onChange({ ...data, ...partial });
  }

  async function refreshKeys() {
    const items = await api<ApiKeyRecord[]>('/api/v1/api-keys');
    setKeys(items);
  }

  useEffect(() => {
    refreshKeys().catch((err) => setError((err as Error).message));
  }, []);

  async function createKey() {
    setBusy(true);
    setError('');
    try {
      const item = await api<ApiKeyRecord>('/api/v1/api-keys', {
        method: 'POST',
        body: JSON.stringify({ name: keyName || '会议纪要 API Key' }),
      });
      setCreated(item.key || '');
      setKeyName('');
      await refreshKeys();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: string) {
    if (!window.confirm('撤销后外部调用将立即失效，确定吗？')) return;
    setBusy(true);
    try {
      await api(`/api/v1/api-keys/${id}`, { method: 'DELETE' });
      setCreated('');
      await refreshKeys();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h2>服务配置</h2>
      <p className="settings-intro">管理 API 地址、存储目录和任务清理策略。会议纪要对外调用时填写本服务地址和 API Key。</p>
      <label className="setting-row"><span><strong>Web API 地址</strong></span><input value={data.web_api_url || ''} onChange={(e) => patch({ web_api_url: e.target.value })} placeholder="http://localhost:8000" /></label>
      <label className="setting-row"><span><strong>MOSS 服务地址</strong></span><input value={data.moss_url || ''} onChange={(e) => patch({ moss_url: e.target.value })} placeholder="http://localhost:9000" /></label>
      <label className="setting-row"><span><strong>存储目录</strong></span><input value={data.storage_root || ''} onChange={(e) => patch({ storage_root: e.target.value })} /></label>
      <label className="setting-row"><span><strong>临时文件目录</strong></span><input value={data.temp_dir || ''} onChange={(e) => patch({ temp_dir: e.target.value })} /></label>
      <label className="setting-row"><span><strong>封面缓存目录</strong></span><input value={data.cover_dir || ''} onChange={(e) => patch({ cover_dir: e.target.value })} /></label>
      <label className="setting-row"><span><strong>任务保留天数</strong></span><input type="number" value={data.retention_days || 30} onChange={(e) => patch({ retention_days: Number(e.target.value) })} /></label>
      <label className="setting-row"><span><strong>启用自动清理</strong></span><input type="checkbox" checked={data.auto_clean !== false} onChange={(e) => patch({ auto_clean: e.target.checked })} /></label>
      <h3 className="subheading">会议纪要 API Key</h3>
      <p className="settings-intro">外部系统填写本服务地址和密钥即可调用 /api/v1/meetings。未创建任何密钥时，本机网页可直接使用。</p>
      <label className="setting-row">
        <span><strong>新建密钥</strong><small>明文只展示一次，请立即复制</small></span>
        <div className="field-with-action">
          <input value={keyName} onChange={(e) => setKeyName(e.target.value)} placeholder="名称，例如 生产环境" />
          <button type="button" className="outline-button" onClick={createKey} disabled={busy}>生成</button>
        </div>
      </label>
      {created && <p className="inline-warning">新密钥：<code>{created}</code></p>}
      {keys.map((item) => (
        <div className="adapter-row" key={item.id}>
          <strong>{item.name}</strong>
          <small>{item.masked}</small>
          <button type="button" className="text-button" onClick={() => revoke(item.id)} disabled={busy}>撤销</button>
        </div>
      ))}
      {error && <p className="inline-error">{error}</p>}
      <div className="health-box">
        <span className="health-dot" /> PostgreSQL / SQLite 已连接
        <span className="health-dot" /> 当前版本 0.1.0
        <span className="health-dot" /> 数据库表已自动迁移
      </div>
    </>
  );
}
