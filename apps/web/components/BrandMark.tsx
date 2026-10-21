'use client';

/**
 * GooMeeting 品牌标：AI 定稿原图（透明底 PNG，public/goo-logo.png）。
 * 形态与品牌定稿一致；size 控制显示尺寸，导航栏 / 登录页 / 注册页共用。
 */
export default function BrandMark({ size = 31 }: { size?: number }) {
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src="/goo-logo.png"
      alt=""
      width={size}
      height={size}
      draggable={false}
      style={{ flex: 'none', display: 'block', objectFit: 'contain' }}
    />
  );
}
