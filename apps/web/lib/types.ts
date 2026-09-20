export type Platform = {
  id: string;
  name: string;
  short: string;
  logo: string | null;
  featured?: boolean;
};

export type MediaVariant = {
  id: string;
  format: string;
  quality?: string | null;
  media_type: string;
  download_status: string;
  file_size?: number | null;
};

export type Job = {
  id: string;
  video_id: string;
  job_type: string;
  status: string;
  progress: number;
  current_stage: string;
  input_media_type?: string | null;
  fallback_used?: boolean;
  error_code?: string | null;
  error_message?: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  logs?: { at: string; level: string; message: string }[];
  progress_detail?: {
    stage?: string;
    message?: string;
    percent?: number;
    elapsed_seconds?: number;
    audio_seconds?: number | null;
    eta_seconds?: number | null;
  } | null;
};

export type Transcript = {
  id: string;
  model: string;
  language: string;
  text: string;
  segments: {
    start?: number;
    end?: number;
    start_ms?: number;
    end_ms?: number;
    text?: string;
    speaker?: string | null;
  }[];
  created_at?: string;
};

export type Summary = {
  id: string;
  provider: string;
  model: string;
  content_markdown: string;
  template_id?: string | null;
  created_at: string;
};

export type VideoAsset = {
  id: string;
  source_url: string;
  platform?: string | null;
  title: string;
  author?: string | null;
  thumbnail_url?: string | null;
  duration_seconds?: number | null;
  parse_status: string;
  created_at: string;
  last_processed_at?: string | null;
  has_transcript?: boolean;
  has_summary?: boolean;
  warning?: string;
  variants?: MediaVariant[];
  jobs?: Job[];
  transcripts?: Transcript[];
  summaries?: Summary[];
};

export type Template = {
  id: string;
  name: string;
  description: string;
  origin: string;
  sections: { title: string; instruction: string; format?: string; item_format?: string }[];
  show_on_home?: boolean;
  can_delete?: boolean;
};
