const configuredApi = process.env.NEXT_PUBLIC_API_URL;

export const API = configuredApi || (
  typeof window === 'undefined'
    ? 'http://localhost:8000'
    : `${window.location.protocol}//${window.location.hostname}:8000`
);

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

export function uploadFile<T>(file: File, onProgress?: (percent: number) => void, signal?: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('POST', `${API}/api/uploads`);
    request.responseType = 'json';
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress?.(Math.round((event.loaded / event.total) * 100));
    };
    request.onerror = () => reject(new Error('上传失败，请检查本机 API 是否运行'));
    request.onabort = () => reject(new Error('上传已取消'));
    request.onload = () => {
      const payload = request.response || {};
      if (request.status >= 200 && request.status < 300) {
        resolve(payload as T);
        return;
      }
      reject(new Error(payload.detail || `上传失败（${request.status}）`));
    };
    if (signal) {
      if (signal.aborted) request.abort();
      signal.addEventListener('abort', () => request.abort(), { once: true });
    }
    const body = new FormData();
    body.append('file', file);
    request.send(body);
  });
}

export function logoSrc(logo?: string | null) {
  return logo ? `/logos/${logo}` : '/logos/placeholder.svg';
}
