'use client';

import type { ReactNode } from 'react';

export default function LinkParserCard({
  url,
  loading,
  onUrlChange,
  onParse,
  children,
}: {
  url: string;
  loading: boolean;
  onUrlChange: (value: string) => void;
  onParse: () => void;
  children: ReactNode;
}) {
  return (
    <section className="workspace-card">
      <div className="workspace-heading">
        <div>
          <h2>解析视频链接</h2>
          <p>粘贴链接后先查看封面和媒体信息，再选择下载或总结。</p>
        </div>
      </div>
      <div className="url-box">
        <span className="url-icon" aria-hidden>↗</span>
        <input
          value={url}
          onChange={(event) => onUrlChange(event.target.value)}
          onKeyDown={(event) => event.key === 'Enter' && onParse()}
          placeholder="粘贴 B 站、抖音、YouTube 等视频链接"
          aria-label="视频链接"
        />
        <button type="button" onClick={onParse} disabled={loading || !url.trim()}>
          {loading ? '解析中…' : '解析'} <b>→</b>
        </button>
      </div>
      <div className="url-foot">
        <span>解析结果会保存为视频资产，重复链接不会新建重复条目</span>
        <span>Enter 解析</span>
      </div>
      {children}
    </section>
  );
}
