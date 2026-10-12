import { API } from './config';

export type AuthUser = {
  id: string;
  username: string;
  display_name?: string | null;
  role: 'admin' | 'user';
  status: string;
  last_login_at?: string | null;
  created_at?: string;
};

export type PublicConfig = {
  first_user: boolean;
  registration_open: boolean;
};

const ACCESS_KEY = 'vtp.access_token';
const REFRESH_KEY = 'vtp.refresh_token';
const USER_KEY = 'vtp.user';

// ------------------------------------------------------------------ token 存取

export function getAccessToken(): string | null {
  if (typeof window === 'undefined') return null;
  return window.localStorage.getItem(ACCESS_KEY);
}

export function getRefreshToken(): string | null {
  if (typeof window === 'undefined') return null;
  return window.localStorage.getItem(REFRESH_KEY);
}

export function getStoredUser(): AuthUser | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as AuthUser) : null;
  } catch {
    return null;
  }
}

export function storeTokens(access: string, refresh: string, user?: AuthUser) {
  window.localStorage.setItem(ACCESS_KEY, access);
  window.localStorage.setItem(REFRESH_KEY, refresh);
  if (user) window.localStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function clearTokens() {
  window.localStorage.removeItem(ACCESS_KEY);
  window.localStorage.removeItem(REFRESH_KEY);
  window.localStorage.removeItem(USER_KEY);
}

/** 给 <img> 等无法携带 Authorization header 的通道拼 ?token=（渲染时取当前 token）。 */
export function withToken(url?: string | null): string | undefined {
  if (!url) return undefined;
  const token = getAccessToken();
  if (!token) return url;
  const separator = url.includes('?') ? '&' : '?';
  return `${url}${separator}token=${encodeURIComponent(token)}`;
}

// ------------------------------------------------------------------ 端点

async function authFetch<T>(path: string, body?: unknown, token?: string | null): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${API}${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = (payload as { detail?: unknown }).detail;
    throw new Error(typeof detail === 'string' ? detail : `请求失败（${response.status}）`);
  }
  return payload as T;
}

export function fetchPublicConfig(): Promise<PublicConfig> {
  return authFetch<PublicConfig>('/api/auth/public-config');
}

export function login(username: string, password: string): Promise<{ access_token: string; refresh_token: string; user: AuthUser }> {
  return authFetch('/api/auth/login', { username, password });
}

export function register(username: string, password: string, displayName?: string): Promise<{ access_token: string; refresh_token: string; user: AuthUser }> {
  return authFetch('/api/auth/register', { username, password, display_name: displayName || undefined });
}

export function fetchMe(): Promise<AuthUser> {
  return authFetch<AuthUser>('/api/auth/me', undefined, getAccessToken());
}

// ------------------------------------------------------------------ 刷新（单飞）

let refreshInFlight: Promise<boolean> | null = null;

function redirectToLogin() {
  if (typeof window === 'undefined') return;
  if (window.location.pathname.startsWith('/login')) return;
  const next = `${window.location.pathname}${window.location.search}`;
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
}

/**
 * 刷新 access token：并发共享同一次请求；失败即清 token 并跳登录页。
 * 调用方（api 层）在 401 时调用，成功后重放原请求。
 */
export function refreshOnce(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;
  const refresh = getRefreshToken();
  if (!refresh) {
    clearTokens();
    redirectToLogin();
    return Promise.resolve(false);
  }
  refreshInFlight = (async () => {
    try {
      const result = await authFetch<{ access_token: string; refresh_token: string; user: AuthUser }>(
        '/api/auth/refresh',
        { refresh_token: refresh },
      );
      storeTokens(result.access_token, result.refresh_token, result.user);
      return true;
    } catch {
      clearTokens();
      redirectToLogin();
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

export async function logout(): Promise<void> {
  const token = getAccessToken();
  try {
    if (token) await authFetch('/api/auth/logout', {}, token);
  } catch {
    // 登出尽力而为：token 失效也清本地
  }
  clearTokens();
}
