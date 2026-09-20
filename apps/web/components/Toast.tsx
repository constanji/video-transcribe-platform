'use client';

export default function Toast({
  message,
  kind = 'success',
  onClose,
}: {
  message: string;
  kind?: 'success' | 'error';
  onClose: () => void;
}) {
  if (!message) return null;
  return (
    <div className={`toast ${kind}`} role="status">
      <span>{kind === 'error' ? `出错：${message}` : message}</span>
      <button type="button" onClick={onClose} aria-label="关闭提示">×</button>
    </div>
  );
}
