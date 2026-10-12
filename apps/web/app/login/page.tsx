'use client';

import { useEffect, useState } from 'react';
import { useAuth } from '../providers';

export default function LoginPage() {
  const { login, publicConfig } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [next, setNext] = useState('/');

  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get('next');
    if (requested && requested.startsWith('/')) setNext(requested);
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!username.trim() || !password) {
      setError('请输入用户名和密码');
      return;
    }
    setBusy(true);
    setError('');
    try {
      await login(username.trim(), password);
      window.location.assign(next);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <div className="auth-brand">
          <span className="brand-mark">◈</span>
          <strong>MediaFlow</strong>
        </div>
        <h1>登录</h1>
        {publicConfig?.first_user ? (
          <p className="auth-hint">系统还没有任何用户，登录页右侧可注册——首位注册的用户将成为管理员。</p>
        ) : null}
        <label className="field-stack">
          用户名
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            placeholder="用户名"
            autoComplete="username"
            autoFocus
          />
        </label>
        <label className="field-stack">
          密码
          <input
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="密码"
            type="password"
            autoComplete="current-password"
          />
        </label>
        {error ? <p className="auth-error">{error}</p> : null}
        <button className="primary-button" type="submit" disabled={busy}>
          {busy ? '登录中…' : '登录'}
        </button>
        {publicConfig?.registration_open ? (
          <p className="auth-switch">
            还没有账号？<a href="/register">注册一个新账号</a>
          </p>
        ) : (
          <p className="auth-switch muted">注册已关闭，请联系管理员</p>
        )}
      </form>
    </div>
  );
}
