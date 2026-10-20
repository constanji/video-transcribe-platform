'use client';

import type { ReactNode } from 'react';

/**
 * GooMeeting 全站线性图标集：2px 圆头描边、24 网格、currentColor 取色。
 * 替代站内所有 Unicode 字符占位（◉ ◈ ✎ × ⌄ → ‹ ›），跨平台渲染一致。
 */
export type IconName =
  | 'wave' | 'play' | 'mic' | 'doc'
  | 'pencil' | 'trash' | 'copy' | 'export'
  | 'chevron-down' | 'chevron-left' | 'chevron-right' | 'arrow-right'
  | 'close' | 'empty';

const GLYPHS: Record<IconName, ReactNode> = {
  wave: (
    <>
      <line x1="4" y1="9.5" x2="4" y2="14.5" />
      <line x1="9.3" y1="5" x2="9.3" y2="19" />
      <line x1="14.6" y1="7.5" x2="14.6" y2="16.5" />
      <line x1="20" y1="10.5" x2="20" y2="13.5" />
    </>
  ),
  play: (
    <>
      <circle cx="12" cy="12" r="9.5" />
      <path d="M10 8.5L16.2 12L10 15.5Z" fill="currentColor" stroke="none" />
    </>
  ),
  mic: (
    <>
      <rect x="9" y="2.5" width="6" height="11.5" rx="3" />
      <path d="M5 11.5v1a7 7 0 0 0 14 0v-1" />
      <line x1="12" y1="19.5" x2="12" y2="21.5" />
      <line x1="8" y1="21.5" x2="16" y2="21.5" />
    </>
  ),
  doc: (
    <>
      <rect x="6" y="3" width="12" height="18" rx="2.5" />
      <line x1="9" y1="8" x2="15" y2="8" />
      <line x1="9" y1="12" x2="15" y2="12" />
      <line x1="9" y1="16" x2="13" y2="16" />
    </>
  ),
  pencil: <path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5L2 22l1.5-5.5L17 3z" />,
  trash: (
    <>
      <path d="M3 6h18" />
      <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <line x1="10" y1="11" x2="10" y2="17" />
      <line x1="14" y1="11" x2="14" y2="17" />
    </>
  ),
  copy: (
    <>
      <rect x="9" y="9" width="12" height="12" rx="2.5" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </>
  ),
  export: (
    <>
      <path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
      <line x1="12" y1="3.5" x2="12" y2="14" />
      <polyline points="7.5 8 12 3.5 16.5 8" />
    </>
  ),
  'chevron-down': <polyline points="6 9 12 15 18 9" />,
  'chevron-left': <polyline points="14.5 6 8.5 12 14.5 18" />,
  'chevron-right': <polyline points="9.5 6 15.5 12 9.5 18" />,
  'arrow-right': (
    <>
      <line x1="4.5" y1="12" x2="19" y2="12" />
      <polyline points="13.5 6.5 19 12 13.5 17.5" />
    </>
  ),
  close: (
    <>
      <line x1="5.5" y1="5.5" x2="18.5" y2="18.5" />
      <line x1="18.5" y1="5.5" x2="5.5" y2="18.5" />
    </>
  ),
  empty: (
    <>
      <rect x="2.5" y="2.5" width="19" height="19" rx="7" fill="var(--accent-soft, #edf5ff)" stroke="none" />
      <path d="M15.9 7.6A6.2 6.2 0 1 0 17.9 12L13 12" strokeWidth="2.1" />
    </>
  ),
};

export default function Icon({
  name,
  size = 18,
  strokeWidth = 2,
  className,
}: {
  name: IconName;
  size?: number;
  strokeWidth?: number;
  className?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      focusable="false"
      style={{ flex: 'none', display: 'inline-block', verticalAlign: '-0.18em' }}
    >
      {GLYPHS[name]}
    </svg>
  );
}
