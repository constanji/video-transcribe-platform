'use client';

import { useEffect, useState } from 'react';

const MAX_LEN = 2000;
const PRESETS_KEY = 'bizctx:presets';

type BizPreset = { id: string; name: string; content: string; updatedAt: string };

function loadPresets(): BizPreset[] {
  try {
    const raw = window.localStorage.getItem(PRESETS_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list.filter((item) => item && item.id && item.content) : [];
  } catch {
    return [];
  }
}

function persistPresets(list: BizPreset[]) {
  try {
    window.localStorage.setItem(PRESETS_KEY, JSON.stringify(list));
  } catch {
    // localStorage 不可用时预设仅存活于当前会话。
  }
}

/**
 * 业务背景补充：生成纪要前填写参会人员准确姓名、公司名、专有名词等，
 * 作为 prompt 注入总结模型，纠正转写中的同音错别字。
 * 草稿按 key（会议/视频 id）持久化到 localStorage，换模板重试不丢；
 * 已填内容可另存为「预设」，跨会议复用，下次总结时直接选用。
 */
export default function BusinessContextInput({
  storageKey,
  onToast,
}: {
  storageKey: string;
  onToast?: (message: string, kind?: 'success' | 'error') => void;
}) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [presets, setPresets] = useState<BizPreset[]>([]);
  const [presetName, setPresetName] = useState('');

  useEffect(() => {
    setLoaded(false);
    try {
      setValue(window.localStorage.getItem(`bizctx:${storageKey}`) || '');
    } catch {
      setValue('');
    }
    setLoaded(true);
    setPresets(loadPresets());
  }, [storageKey]);

  function update(next: string) {
    const clipped = next.slice(0, MAX_LEN);
    setValue(clipped);
    try {
      window.localStorage.setItem(`bizctx:${storageKey}`, clipped);
    } catch {
      // localStorage 不可用时仅保留在内存，本次会话内仍然生效。
    }
  }

  function current() {
    return (loaded ? value : '').trim();
  }

  function savePreset() {
    const content = current();
    if (!content) {
      onToast?.('请先填写业务背景再保存为预设', 'error');
      return;
    }
    const name = (presetName.trim() || `预设 ${new Date().toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}`).slice(0, 40);
    setPresets((list) => {
      // 同名覆盖，避免预设列表越堆越长。
      const existing = list.find((item) => item.name === name);
      const next = existing
        ? list.map((item) => (item.name === name ? { ...item, content, updatedAt: new Date().toISOString() } : item))
        : [...list, { id: `p-${Date.now()}`, name, content, updatedAt: new Date().toISOString() }];
      persistPresets(next);
      return next;
    });
    setPresetName('');
    onToast?.(presets.some((item) => item.name === name) ? `预设「${name}」已更新` : `已保存预设「${name}」`);
  }

  function applyPreset(id: string) {
    if (!id) return;
    const preset = presets.find((item) => item.id === id);
    if (!preset) return;
    update(preset.content);
    onToast?.(`已载入预设「${preset.name}」`);
  }

  function deletePreset(id: string) {
    const preset = presets.find((item) => item.id === id);
    setPresets((list) => {
      const next = list.filter((item) => item.id !== id);
      persistPresets(next);
      return next;
    });
    if (preset) onToast?.(`已删除预设「${preset.name}」`);
  }

  return (
    <div className="biz-context">
      <div className="biz-context-head">
        <button
          type="button"
          className="text-button"
          onClick={() => setOpen((current) => !current)}
          aria-expanded={open}
        >
          {open ? '▾' : '▸'} 业务背景补充{current() ? <em className="biz-context-dot" title="已填写" /> : null}
        </button>
        <small>可选：填写参会人员准确姓名、公司名、专有名词，生成纪要时自动纠正转写错别字</small>
      </div>
      {open && (
        <div className="biz-context-body">
          <textarea
            value={value}
            maxLength={MAX_LEN}
            rows={4}
            placeholder={'示例：\n参会人：张伟（销售部）、郭建平（技术部）\n公司：深圳市固高科技股份有限公司（简称固高）\n术语：MOSS（本地转录服务）、领元Agent（公司产品名）'}
            onChange={(event) => update(event.target.value)}
          />
          <div className="biz-context-foot">
            <small>{value.length}/{MAX_LEN} · 自动保存草稿，仅用于本次纪要生成，不会进入转录文本</small>
            {current() && (
              <button type="button" className="text-button" onClick={() => { update(''); onToast?.('已清空业务背景'); }}>
                清空
              </button>
            )}
          </div>
          <div className="biz-context-section-label">保存为预设，下次直接选用</div>
          <div className="biz-context-save">
            <input
              value={presetName}
              maxLength={40}
              placeholder="预设名称（如：固高周会参会人），留空按时间命名"
              aria-label="预设名称"
              onChange={(event) => setPresetName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  savePreset();
                }
              }}
            />
            <button type="button" className="outline-button" onClick={savePreset} disabled={!current()}>
              存为预设
            </button>
          </div>
          {presets.length > 0 && (
            <>
              <div className="biz-context-section-label">已保存预设（点击名称载入）</div>
              <ul className="biz-context-preset-list">
                {presets.map((item) => (
                  <li key={item.id}>
                    <button
                      type="button"
                      className="text-button"
                      title={item.content.slice(0, 80)}
                      onClick={() => applyPreset(item.id)}
                    >
                      {item.name}
                    </button>
                    <small>{new Date(item.updatedAt).toLocaleDateString('zh-CN')}</small>
                    <button type="button" className="text-button danger" aria-label={`删除预设 ${item.name}`} onClick={() => deletePreset(item.id)}>
                      删除
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** 读取某 key 当前已填写的业务背景（提交时取值用）。 */
export function readBusinessContext(storageKey: string) {
  try {
    return (window.localStorage.getItem(`bizctx:${storageKey}`) || '').trim();
  } catch {
    return '';
  }
}
