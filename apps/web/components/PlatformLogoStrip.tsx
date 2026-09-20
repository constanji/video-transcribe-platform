'use client';

import { useMemo, useState } from 'react';
import { logoSrc } from '../lib/api';
import type { Platform } from '../lib/types';

const FEATURED = ['bilibili', 'douyin', 'kuaishou', 'weibo', 'xhs', 'youtube', 'tiktok', 'zhihu'];

export default function PlatformLogoStrip({ platforms }: { platforms: Platform[] }) {
  const [expanded, setExpanded] = useState(false);
  const featured = useMemo(
    () => FEATURED.map((id) => platforms.find((item) => item.id === id)).filter(Boolean) as Platform[],
    [platforms],
  );
  const extra = platforms.filter((item) => !FEATURED.includes(item.id));
  const visible = expanded ? [...featured, ...extra] : featured;

  return (
    <div className="platform-strip">
      <span className="platform-strip-label">支持的平台</span>
      <div className="platform-logos">
        {visible.map((platform) => (
          <span className="platform-logo" key={platform.id} title={platform.name}>
            <img src={logoSrc(platform.logo)} alt="" width={20} height={20} />
            <em>{platform.name}</em>
          </span>
        ))}
      </div>
      {extra.length > 0 && (
        <button type="button" className="more-platforms" onClick={() => setExpanded((value) => !value)}>
          {expanded ? '收起' : `更多平台（${extra.length}）`}
        </button>
      )}
    </div>
  );
}
