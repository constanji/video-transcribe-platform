import type { VideoAsset } from './types';

/** Pixiv 动图：源是 zip 帧包，预览/下载时按需合成为 GIF，UI 上按图片处理。 */
export function isUgoira(item: { media_type?: string | null; format?: string | null }) {
  return item.media_type === 'file' && (item.format || '').toLowerCase() === 'zip';
}

/** 只有图片（含动图）、没有视频或音频时，按图片解析记录处理。 */
export function isImageRecord(video: VideoAsset) {
  const variants = video.variants || [];
  const hasImage = variants.some((item) => item.media_type === 'image' || isUgoira(item));
  const hasPlayable = variants.some((item) => item.media_type === 'video' || item.media_type === 'audio');
  return hasImage && !hasPlayable;
}
