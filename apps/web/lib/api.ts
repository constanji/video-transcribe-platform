import { getAccessToken, refreshOnce } from './auth';
import { API } from './config';

export { API };

function isAuthPath(path: string) {
  return path.startsWith('/api/auth/');
}

async function send(path: string, init?: RequestInit, withAuth = true): Promise<Response> {
  const headers = new Headers(init?.headers || {});
  if (!headers.has('Content-Type') && init?.body && !(init.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }
  if (withAuth) {
    const token = getAccessToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);
  }
  try {
    return await fetch(`${API}${path}`, { ...init, headers });
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw error;
    throw new Error('无法连接 API 服务，请检查网络或确认本机服务正在运行');
  }
}

async function parseError(response: Response): Promise<Error> {
  let message = '请求失败';
  try {
    const body = await response.json();
    const detail = body.detail || body.error_message || body.message;
    if (typeof detail === 'string') message = detail;
    else if (Array.isArray(detail)) {
      message = detail.map((item) => item?.msg || String(item)).join('；');
    }
  } catch {
    message = `${message}（${response.status}）`;
  }
  return new Error(message);
}

/** 统一请求：自动带 Bearer；401 时单飞刷新并重放一次，仍失败抛错。 */
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let response = await send(path, init);
  if (response.status === 401 && !isAuthPath(path)) {
    if (await refreshOnce()) {
      response = await send(path, init);
    }
  }
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return response.json();
}

/** FormData 上传（不设 Content-Type，交给浏览器带 boundary），同样支持 401 刷新重放。 */
export async function apiForm<T>(path: string, form: FormData): Promise<T> {
  let response = await send(path, { method: 'POST', body: form });
  if (response.status === 401 && !isAuthPath(path)) {
    if (await refreshOnce()) {
      response = await send(path, { method: 'POST', body: form });
    }
  }
  if (!response.ok) throw await parseError(response);
  return response.json();
}

export function uploadFileTo<T>(
  path: string,
  file: File,
  onProgress?: (percent: number) => void,
  signal?: AbortSignal,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('POST', `${API}${path}`);
    request.responseType = 'json';
    const token = getAccessToken();
    if (token) request.setRequestHeader('Authorization', `Bearer ${token}`);
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress?.(Math.round((event.loaded / event.total) * 100));
    };
    request.onerror = () => reject(new Error('上传失败，请检查本机 API 是否运行'));
    request.onabort = () => reject(new Error('上传已取消'));
    request.onload = async () => {
      const payload = request.response || {};
      if (request.status >= 200 && request.status < 300) {
        resolve(payload as T);
        return;
      }
      // 401：刷新 token 后重发一次（FormData 已消费，需重建）。
      if (request.status === 401 && (await refreshOnce())) {
        const retry = new XMLHttpRequest();
        retry.open('POST', `${API}${path}`);
        retry.responseType = 'json';
        retry.setRequestHeader('Authorization', `Bearer ${getAccessToken()}`);
        retry.upload.onprogress = (event) => {
          if (event.lengthComputable) onProgress?.(Math.round((event.loaded / event.total) * 100));
        };
        retry.onload = () => {
          const retryPayload = retry.response || {};
          if (retry.status >= 200 && retry.status < 300) resolve(retryPayload as T);
          else reject(new Error(retryPayload.detail || `上传失败（${retry.status}）`));
        };
        retry.onerror = () => reject(new Error('上传失败，请检查本机 API 是否运行'));
        retry.onabort = () => reject(new Error('上传已取消'));
        if (signal) {
          if (signal.aborted) retry.abort();
          else signal.addEventListener('abort', () => retry.abort(), { once: true });
        }
        const retryBody = new FormData();
        retryBody.append('file', file);
        retry.send(retryBody);
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

export function uploadFile<T>(file: File, onProgress?: (percent: number) => void, signal?: AbortSignal): Promise<T> {
  return uploadFileTo<T>('/api/uploads', file, onProgress, signal);
}

/** 带认证的原始 Response（调用方自行处理 blob/文件名等），401 时刷新重放一次。 */
export async function apiRaw(path: string, init?: RequestInit): Promise<Response> {
  let response = await send(path, init);
  if (response.status === 401 && !isAuthPath(path)) {
    if (await refreshOnce()) {
      response = await send(path, init);
    }
  }
  return response;
}

/** 带认证的二进制下载（浏览器直达无法带 header，用 fetch + blob）。 */
export async function downloadFile(url: string, filename: string): Promise<void> {
  const response = await apiRaw(url.startsWith('http') ? url.replace(API, '') : url);
  if (!response.ok) throw await parseError(response);
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 10_000);
}

export function logoSrc(logo?: string | null) {
  return logo ? `/logos/${logo}` : '/logos/placeholder.svg';
}
