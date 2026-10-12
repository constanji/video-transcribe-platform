'use client';

import { useEffect, useState } from 'react';
import { useAuth } from '../providers';

export default function RegisterPage() {
  const { register, publicConfig } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [next, setNext] = useState('/');
  const [closed, setClosed] = useState(false);

  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get('next');
    if (requested && requested.startsWith('/')) setNext(requested);
  }, []);

  useEffect(() => {
    // 首位用户永远开放；否则跟随 public-config。
    if (publicConfig && !publicConfig.first_user && !publicConfig.registration_open) {
      setClosed(true);
    }
  }, [publicConfig]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!username.trim() || !password) {
      setError('请输入用户名和密码');
      return;
    }
    if (password !== confirm) {
      setError('两次输入的密码不一致');
      return;
    }
    if (password.length < 8) {
      setError('密码至少 8 位');
      return;
    }
    setBusy(true);
    setError('');
    try {
      await register(username.trim(), password, displayName.trim() || undefined);
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
        <h1>注册</h1>
        {closed ? (
          <p className="auth-hint">注册已关闭，请联系管理员开通账号。</p>
        ) : publicConfig?.first_user ? (
          <p className="auth-hint">你是系统第一位用户，注册后将成为管理员，历史数据会归属到你名下。</p>
        ) : null}
        <label className="field-stack">
          用户名
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            placeholder="字母、数字、_ . - ，3-80 位"
            autoComplete="username"
            autoFocus
            disabled={closed}
          />
        </label>
        <label className="field-stack">
          昵称（选填）
          <input
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            placeholder="展示用名称"
            autoComplete="nickname"
            disabled={closed}
          />
        </label>
        <label className="field-stack">
          密码
          <input
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="至少 8 位"
            type="password"
            autoComplete="new-password"
            disabled={closed}
          />
        </label>
        <label className="field-stack">
          确认密码
          <input
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
            placeholder="再输入一次"
            type="password"
            autoComplete="new-password"
            disabled={closed}
          />
        </label>
        {error ? <p className="auth-error">{error}</p> : null}
        <button className="primary-button" type="submit" disabled={busy || closed}>
          {busy ? '注册中…' : '注册并登录'}
        </button>
        <p className="auth-switch">
          已有账号？<a href="/login">直接登录</a>
        </p>
      </form>
    </div>
  );
}
