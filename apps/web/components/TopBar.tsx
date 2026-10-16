'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { api } from '../lib/api';
import { useAuth } from '../app/providers';

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

export default function TopBar({ active }: { active: 'meeting' | 'parse' | 'history' | 'settings' }) {
  const [health, setHealth] = useState<MossHealth | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const { user, logout } = useAuth();
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

  useEffect(() => {
    if (!menuOpen) return;
    function onDocClick(event: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) setMenuOpen(false);
    }
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [menuOpen]);

  return (
    <header className="topbar">
      <Link className="brand" href="/">
        <span className="brand-mark">◈</span>
        <span>MediaFlow</span>
      </Link>
      <nav className="nav-tabs">
        {/* 用 next/link 而非 <a>：切页走客户端路由，保住录音中的 MediaStream/WebSocket 等页面外状态。 */}
        <Link className={active === 'meeting' ? 'active' : ''} href="/">会议纪要</Link>
        <Link className={active === 'parse' ? 'active' : ''} href="/parse">通用解析</Link>
        <Link className={active === 'history' ? 'active' : ''} href="/history">历史解析任务</Link>
        <Link className={active === 'settings' ? 'active' : ''} href="/settings">设置</Link>
      </nav>
      <div className={`service-state ${state.kind}`}><span /> {state.text}</div>
      {user ? (
        <div className="user-chip" ref={menuRef}>
          <button type="button" onClick={() => setMenuOpen((open) => !open)}>
            <span className="user-avatar">{(user.display_name || user.username).slice(0, 1).toUpperCase()}</span>
            <span className="user-name">{user.display_name || user.username}</span>
            {user.role === 'admin' ? <em className="user-role">管理员</em> : null}
          </button>
          {menuOpen ? (
            <div className="user-menu">
              <div className="user-menu-head">
                <strong>{user.display_name || user.username}</strong>
                <small>@{user.username}</small>
              </div>
              <button type="button" onClick={() => logout()}>退出登录</button>
            </div>
          ) : null}
        </div>
      ) : null}
    </header>
  );
}
