'use client';

import { useEffect, useState } from 'react';
import { api } from '../lib/api';

type MossHealth = {
  moss_reachable?: boolean;
  moss_model_loaded?: boolean;
  moss_model_loading?: boolean;
  moss_warmup_error?: string | null;
};

function mossLabel(health: MossHealth | null) {
  if (!health) return { text: '检测服务中', kind: 'loading' };
  if (!health.moss_reachable) return { text: '转录服务未连接', kind: 'warn' };
  if (health.moss_model_loaded) return { text: '模型已就绪', kind: 'ok' };
  if (health.moss_model_loading) return { text: '模型加载中', kind: 'loading' };
  if (health.moss_warmup_error) return { text: '模型加载失败', kind: 'error' };
  return { text: '模型未加载', kind: 'warn' };
}

export default function TopBar({ active }: { active: 'workbench' | 'history' | 'settings' }) {
  const [health, setHealth] = useState<MossHealth | null>(null);
  const state = mossLabel(health);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const next = await api<MossHealth>('/api/system/health');
        if (!cancelled) setHealth(next);
      } catch {
        if (!cancelled) setHealth({ moss_reachable: false });
      }
    }
    tick();
    const timer = window.setInterval(tick, health?.moss_model_loading || !health?.moss_model_loaded ? 3000 : 15000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [health?.moss_model_loading, health?.moss_model_loaded]);

  return (
    <header className="topbar">
      <a className="brand" href="/">
        <span className="brand-mark">◈</span>
        <span>MediaFlow</span>
      </a>
      <nav className="nav-tabs">
        <a className={active === 'workbench' ? 'active' : ''} href="/">工作台</a>
        <a className={active === 'history' ? 'active' : ''} href="/history">历史解析任务</a>
        <a className={active === 'settings' ? 'active' : ''} href="/settings">设置</a>
      </nav>
      <div className={`service-state ${state.kind}`}><span /> {state.text}</div>
    </header>
  );
}
