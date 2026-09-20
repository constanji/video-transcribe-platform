const KEY = 'mediaflow.workbench.draft';

export type WorkbenchPanel = 'download' | 'summary' | 'transcribe' | 'summarize' | null;

export type WorkbenchDraft = {
  url: string;
  videoId?: string;
  panel?: WorkbenchPanel;
};

export function readWorkbenchDraft(): WorkbenchDraft {
  if (typeof window === 'undefined') return { url: '' };
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return { url: '' };
    const parsed = JSON.parse(raw) as WorkbenchDraft;
    return {
      url: typeof parsed.url === 'string' ? parsed.url : '',
      videoId: parsed.videoId || undefined,
      panel: parsed.panel === 'download' || parsed.panel === 'summary' || parsed.panel === 'transcribe' || parsed.panel === 'summarize' ? parsed.panel : null,
    };
  } catch {
    return { url: '' };
  }
}

export function writeWorkbenchDraft(partial: Partial<WorkbenchDraft>) {
  if (typeof window === 'undefined') return;
  const current = readWorkbenchDraft();
  const next: WorkbenchDraft = {
    url: partial.url ?? current.url,
    videoId: partial.videoId === undefined ? current.videoId : partial.videoId,
    panel: partial.panel === undefined ? current.panel : partial.panel,
  };
  window.localStorage.setItem(KEY, JSON.stringify(next));
}
