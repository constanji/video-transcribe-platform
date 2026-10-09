import { API, api } from './api';
import type { MeetingHistoryItem, MeetingRecord } from './types';

export function meetingWsUrl(meetingId: string) {
  const wsBase = API.replace(/^http/, 'ws');
  return `${wsBase}/api/v1/meetings/${meetingId}/live`;
}

export function listMeetings() {
  return api<MeetingHistoryItem[]>('/api/v1/meetings');
}

export function createMeeting(body: { title?: string; mode: 'live' | 'recording'; language: string; audio_source: string }) {
  return api<MeetingRecord>('/api/v1/meetings', { method: 'POST', body: JSON.stringify(body) });
}

export function getMeeting(id: string) {
  return api<MeetingRecord>(`/api/v1/meetings/${id}`);
}

export function stopMeeting(id: string) {
  return api<MeetingRecord>(`/api/v1/meetings/${id}/stop`, { method: 'POST' });
}

export function retryTranscribe(id: string) {
  return api<MeetingRecord>(`/api/v1/meetings/${id}/retry-transcribe`, { method: 'POST' });
}

export function cancelJob(id: string) {
  return api<any>(`/api/jobs/${id}`, { method: 'DELETE' });
}

export function uploadMeetingAudio(id: string, file: File, transcribe = true) {
  const body = new FormData();
  body.append('file', file);
  return fetch(`${API}/api/v1/meetings/${id}/audio?transcribe=${transcribe ? 'true' : 'false'}`, {
    method: 'POST',
    body,
  }).then(async (response) => {
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `上传失败（${response.status}）`);
    return payload as MeetingRecord;
  });
}
