import type { VideoAsset } from './types';

/** 只有图片、没有视频或音频时，按图片解析记录处理。 */
export function isImageRecord(video: VideoAsset) {
  const variants = video.variants || [];
  const hasImage = variants.some((item) => item.media_type === 'image');
  const hasPlayable = variants.some((item) => item.media_type === 'video' || item.media_type === 'audio');
  return hasImage && !hasPlayable;
}
