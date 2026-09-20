'use client';

import { useEffect, useState } from 'react';
import { api } from '../lib/api';

type MossHealth = {
  moss_reachable?: boolean;
  moss_model_found?: boolean;
  moss_model_loaded?: boolean;
  moss_model_loading?: boolean;
  moss_device?: string | null;
  moss_warmup_error?: string | null;
};

function statusText(health: MossHealth | null, fallbackFound: boolean) {
  if (!health) return fallbackFound ? '模型文件已找到' : '未检测';
  if (!health.moss_reachable) return '转录服务未连接';
  if (health.moss_model_loaded) return health.moss_device ? `模型已就绪 · ${health.moss_device}` : '模型已就绪';
  if (health.moss_model_loading) return '模型加载中，首次可能需要几分钟';
  if (health.moss_warmup_error) return `模型加载失败：${health.moss_warmup_error}`;
  if (health.moss_model_found) return '模型文件已找到，尚未加载到内存';
  return '未找到 MOSS 模型文件';
}

export default function TranscriptionSettings({
  value,
  onChange,
  onToast,
}: {
  value: any;
  onChange: (value: any) => void;
  onToast: (message: string, kind?: 'success' | 'error') => void;
}) {
  const data = value || {};
  const [health, setHealth] = useState<MossHealth | null>(null);
  const [warming, setWarming] = useState(false);

  function patch(partial: Record<string, unknown>) {
    onChange({ ...data, ...partial });
  }

  async function refreshHealth() {
    const result = await api<MossHealth>('/api/system/health');
    setHealth(result);
    return result;
  }

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const result = await api<MossHealth>('/api/system/health');
        if (!cancelled) setHealth(result);
      } catch {
        if (!cancelled) setHealth({ moss_reachable: false });
      }
    }
    tick();
    const timer = window.setInterval(tick, health?.moss_model_loading ? 3000 : 12000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [health?.moss_model_loading]);

  async function testMoss() {
    try {
      const result = await refreshHealth();
      const message = statusText(result, Boolean(data.moss_model_found));
      onToast(message, result.moss_reachable ? 'success' : 'error');
    } catch (err) {
      onToast((err as Error).message, 'error');
    }
  }

  async function warmup() {
    setWarming(true);
    try {
      const result = await api<any>('/api/system/moss/warmup', { method: 'POST' });
      await refreshHealth();
      onToast(result.device ? `模型已就绪 · ${result.device}` : '模型已就绪', 'success');
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setWarming(false);
    }
  }

  const loaded = Boolean(health?.moss_model_loaded);
  const loading = Boolean(health?.moss_model_loading) || warming;

  return (
    <>
      <h2>转录分析</h2>
      <p className="settings-intro">配置 MOSS 转录、音频回退策略和总结模型。服务启动后会自动预热模型，无需等第一次总结再加载。</p>
      <h3 className="subheading">转录配置</h3>
      <label className="setting-row">
        <span><strong>MOSS 服务 URL</strong><small>当前状态：{statusText(health, Boolean(data.moss_model_found))}</small></span>
        <div className="field-with-action">
          <input value={data.moss_url || ''} onChange={(e) => patch({ moss_url: e.target.value })} placeholder="http://localhost:9000" />
          <button type="button" className="outline-button" onClick={testMoss}>测试连接</button>
        </div>
      </label>
      <label className="setting-row">
        <span><strong>模型预热</strong><small>{loaded ? '已驻留内存，后续任务直接复用' : '预热会等待加载完成，之后总结不再承担这段等待'}</small></span>
        <button type="button" className="outline-button" onClick={warmup} disabled={loaded || loading}>
          {loaded ? '已就绪' : loading ? '加载中…' : '立即预热'}
        </button>
      </label>
      <label className="setting-row">
        <span><strong>MOSS 转录模型</strong><small>{data.moss_model_found ? '已找到 Meetily 本机模型，可直接使用' : '未找到模型文件'}</small></span>
        <input value={data.moss_model_path || ''} onChange={(e) => patch({ moss_model_path: e.target.value })} />
      </label>
      <label className="setting-row"><span><strong>默认模型名称</strong></span><input value={data.default_model || ''} onChange={(e) => patch({ default_model: e.target.value })} /></label>
      <label className="setting-row">
        <span><strong>默认语言</strong></span>
        <select value={data.default_language || 'zh'} onChange={(e) => patch({ default_language: e.target.value })}>
          <option value="zh">中文</option>
          <option value="auto">自动识别</option>
          <option value="en">英语</option>
        </select>
      </label>
      <label className="setting-row"><span><strong>音频提取格式</strong></span><input value={data.audio_format || 'wav'} onChange={(e) => patch({ audio_format: e.target.value })} /></label>
      <label className="setting-row"><span><strong>音频采样率</strong></span><input type="number" value={data.sample_rate || 16000} onChange={(e) => patch({ sample_rate: Number(e.target.value) })} /></label>
      <label className="setting-row"><span><strong>失败时回退 MP4</strong></span><input type="checkbox" checked={data.fallback_to_mp4 !== false} onChange={(e) => patch({ fallback_to_mp4: e.target.checked })} /></label>
      <label className="setting-row"><span><strong>最大并发转录任务</strong></span><input type="number" value={data.max_concurrency || 1} onChange={(e) => patch({ max_concurrency: Number(e.target.value) })} /></label>
      <label className="setting-row"><span><strong>保留时间戳</strong></span><input type="checkbox" checked={data.keep_timestamps !== false} onChange={(e) => patch({ keep_timestamps: e.target.checked })} /></label>
      <h3 className="subheading">总结配置</h3>
      <label className="setting-row">
        <span><strong>默认 Provider</strong></span>
        <select value={data.default_provider || 'local'} onChange={(e) => patch({ default_provider: e.target.value })}>
          <option value="local">内置本地模型</option>
          <option value="ollama">Ollama</option>
          <option value="openai">OpenAI 兼容 API</option>
        </select>
      </label>
      <label className="setting-row">
        <span><strong>总结小模型</strong><small>{data.summary_model_found ? '已找到 Qwen3.5-4B-Q4_K_M，可直接使用' : '未找到 GGUF 文件'}</small></span>
        <input value={data.summary_model || ''} onChange={(e) => patch({ summary_model: e.target.value })} />
      </label>
      <label className="setting-row">
        <span><strong>总结模型路径</strong></span>
        <input value={data.summary_model_path || ''} onChange={(e) => patch({ summary_model_path: e.target.value })} />
      </label>
      <label className="setting-row"><span><strong>API Base URL</strong></span><input value={data.api_base_url || ''} onChange={(e) => patch({ api_base_url: e.target.value })} /></label>
      <label className="setting-row"><span><strong>API Key</strong><small>脱敏展示，留空表示不修改</small></span><input value={data.api_key || ''} onChange={(e) => patch({ api_key: e.target.value })} placeholder={data.api_key_masked || '未配置'} /></label>
      <label className="setting-row"><span><strong>请求超时（秒）</strong></span><input type="number" value={data.timeout || 60} onChange={(e) => patch({ timeout: Number(e.target.value) })} /></label>
      <label className="setting-row"><span><strong>最大上下文长度</strong></span><input type="number" value={data.max_context || 8000} onChange={(e) => patch({ max_context: Number(e.target.value) })} /></label>
      <label className="setting-row"><span><strong>分段大小</strong></span><input type="number" value={data.chunk_size || 4000} onChange={(e) => patch({ chunk_size: Number(e.target.value) })} /></label>
      <label className="setting-row"><span><strong>重试次数</strong></span><input type="number" value={data.retry_times || 2} onChange={(e) => patch({ retry_times: Number(e.target.value) })} /></label>
    </>
  );
}
