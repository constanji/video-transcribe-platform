'use client';

import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import type { AuthUser, PublicConfig } from '../lib/auth';
import {
  fetchMe,
  fetchPublicConfig,
  getAccessToken,
  getRefreshToken,
  login as apiLogin,
  register as apiRegister,
  refreshOnce,
  storeTokens,
  logout as apiLogout,
} from '../lib/auth';

type AuthState = {
  user: AuthUser | null;
  loading: boolean;
  publicConfig: PublicConfig | null;
  login: (username: string, password: string) => Promise<AuthUser>;
  register: (username: string, password: string, displayName?: string) => Promise<AuthUser>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthState | null>(null);

const PUBLIC_PATHS = ['/login', '/register'];

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth 必须在 AuthProvider 内使用');
  return context;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [publicConfig, setPublicConfig] = useState<PublicConfig | null>(null);

  useEffect(() => {
    fetchPublicConfig()
      .then(setPublicConfig)
      .catch(() => setPublicConfig({ first_user: true, registration_open: true }));
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      if (!getAccessToken()) {
        if (!cancelled) {
          setUser(null);
          setLoading(false);
        }
        return;
      }
      try {
        let me = await fetchMe();
        if (!me) throw new Error('未登录');
        setUser(me);
        storeTokens(getAccessToken() as string, getRefreshToken() as string, me);
      } catch {
        // access 过期：尝试刷新一次再拉 me；仍失败即未登录（refreshOnce 内部会清 token 并跳登录）。
        try {
          if (await refreshOnce()) {
            const me = await fetchMe();
            if (!cancelled) setUser(me);
          } else if (!cancelled) {
            setUser(null);
          }
        } catch {
          if (!cancelled) setUser(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const result = await apiLogin(username, password);
    storeTokens(result.access_token, result.refresh_token, result.user);
    setUser(result.user);
    return result.user;
  }, []);

  const register = useCallback(async (username: string, password: string, displayName?: string) => {
    const result = await apiRegister(username, password, displayName);
    storeTokens(result.access_token, result.refresh_token, result.user);
    setUser(result.user);
    return result.user;
  }, []);

  const logout = useCallback(async () => {
    await apiLogout();
    setUser(null);
    window.location.assign('/login');
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, publicConfig, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function AuthGate({ children }: { children: React.ReactNode }) {
  const { loading, user } = useAuth();
  // path 只在挂载后读取：SSR/首帧不碰 window（避免 next build 预渲染崩溃）。
  const [path, setPath] = useState('');
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setPath(window.location.pathname);
  }, []);

  useEffect(() => {
    if (loading || !path) return;
    const isPublic = PUBLIC_PATHS.some((item) => path.startsWith(item));
    if (!user && !isPublic) {
      const next = `${window.location.pathname}${window.location.search}`;
      window.location.replace(`/login?next=${encodeURIComponent(next)}`);
      return;
    }
    if (user && isPublic) {
      // 与登录/注册页成功后的 assign(next) 存在竞争：这里也走 next，
      // 保证无论谁先执行，最终都落在深链目标上（next 是公开页则回首页防环）。
      const requested = new URLSearchParams(window.location.search).get('next');
      const safeNext = requested && requested.startsWith('/') && !PUBLIC_PATHS.some((item) => requested.startsWith(item)) ? requested : '/';
      window.location.replace(safeNext);
      return;
    }
    setReady(true);
  }, [loading, user, path]);

  const waiting = loading || !path || (!ready && !PUBLIC_PATHS.some((item) => path.startsWith(item)));
  if (waiting) {
    return (
      <div className="auth-gate">
        <div className="auth-gate-card">
          <span className="brand-mark">◈</span>
          <p>正在加载 MediaFlow…</p>
        </div>
      </div>
    );
  }
  return <>{children}</>;
}
