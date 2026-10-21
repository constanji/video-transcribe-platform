'use client';

import { useEffect, useRef, useState } from 'react';
import { meetingSession } from '../lib/meeting-session';

// 按逗号/顿号/分号/换行切词，给按钮徽标和卡片提示展示词数。
function countWords(value: string) {
  return value
    .split(/[,，、;；\n]+/)
    .map((word) => word.trim())
    .filter(Boolean).length;
}

/**
 * 热词控件：控制行里是一枚状态按钮（开/关样式区分），点击弹出配置卡片。
 * 卡片内编辑热词清单 + 「启用热词」开关；外点或 Esc 关闭。
 */
export default function HotwordControl({
  hotwords,
  enabled,
  locked,
}: {
  hotwords: string;
  enabled: boolean;
  locked: boolean;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDocDown = (event: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDocDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const words = countWords(hotwords);

  return (
    <div className={`hotword-wrap${open ? ' is-open' : ''}`} ref={wrapRef}>
      <button
        type="button"
        className={`hotword-trigger${enabled ? ' is-on' : ''}`}
        disabled={locked}
        title="转录热词：人名、产品名、术语等，可提高这些词的识别准确率"
        onClick={() => setOpen((value) => !value)}
      >
        <span>热词</span>
        <b className="hotword-badge">{enabled ? (words > 0 ? `${words} 词` : '未填写') : '关闭'}</b>
      </button>
      {open && (
        <div className="hotword-card" role="dialog" aria-label="转录热词配置">
          <div className="hotword-card-head">
            <strong>转录热词</strong>
            <button
              type="button"
              className={`hotword-switch${enabled ? ' is-on' : ''}`}
              role="switch"
              aria-checked={enabled}
              aria-label="启用热词"
              title={enabled ? '点击停用热词' : '点击启用热词'}
              onClick={() => meetingSession.setHotwordsEnabled(!enabled)}
            >
              <i />
            </button>
          </div>
          <textarea
            value={hotwords}
            maxLength={500}
            rows={3}
            placeholder="逗号分隔，如：赵国瑞, 向量数据库, 固高"
            onChange={(event) => meetingSession.setHotwords(event.target.value)}
          />
          <p className="hotword-hint">
            {enabled
              ? `已启用 · ${words} 个词，建议不超过 10 个，仅对该会议的转录生效`
              : '当前未启用，填写内容后打开右上角开关生效'}
          </p>
        </div>
      )}
    </div>
  );
}
