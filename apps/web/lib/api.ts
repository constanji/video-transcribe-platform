export const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  });
  if (!response.ok) {
    let message = '请求失败';
    try {
      const body = await response.json();
      message = body.detail || body.error_message || body.message || message;
    } catch {
      message = `${message}（${response.status}）`;
    }
    throw new Error(typeof message === 'string' ? message : JSON.stringify(message));
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export function logoSrc(logo?: string | null) {
  return logo ? `/logos/${logo}` : '/logos/placeholder.svg';
}
