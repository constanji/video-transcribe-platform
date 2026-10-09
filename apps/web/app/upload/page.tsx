'use client';

import { useEffect, useState } from 'react';
import MeetingWorkbench from '../../components/MeetingWorkbench';
import Toast from '../../components/Toast';
import TopBar from '../../components/TopBar';
import { api } from '../../lib/api';
import type { Platform } from '../../lib/types';

export default function UploadPage() {
  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [toast, setToast] = useState<{ message: string; kind?: 'success' | 'error' }>({ message: '' });

  useEffect(() => {
    api<Platform[]>('/api/platforms').then(setPlatforms).catch(() => setPlatforms([]));
  }, []);

  return (
    <main className="page-shell upload-page">
      <TopBar active="upload" />
      <div className="page-heading">
        <div>
          <span className="eyebrow">MEETING NOTES</span>
          <h1>会议纪要</h1>
          <p>点击开始录音。实时转录会边说边出字；录音转录在停止后整段转写。完成后可选择模板生成纪要。</p>
        </div>
      </div>
      <Toast message={toast.message} kind={toast.kind} onClose={() => setToast({ message: '' })} />
      <MeetingWorkbench platforms={platforms} onToast={(message, kind) => setToast({ message, kind })} />
    </main>
  );
}
