'use client';

/**
 * GooMeeting 品牌标（声纹 G 弧）：蓝色圆角方块 + 断弧横杠构成的抽象 G。
 * 矢量绘制，替代原 Unicode ◈ 占位字符；任意尺寸清晰，与系统字体解耦。
 */
export default function BrandMark({ size = 31 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-hidden="true"
      focusable="false"
      style={{ flex: 'none', display: 'block' }}
    >
      <rect width="32" height="32" rx="9" fill="#2787F5" />
      <path
        d="M22.4 9.6A9 9 0 1 0 25 16L17.5 16"
        fill="none"
        stroke="#fff"
        strokeWidth="2.8"
        strokeLinecap="round"
      />
    </svg>
  );
}
