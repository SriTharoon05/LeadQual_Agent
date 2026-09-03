// ── Auth ────────────────────────────────────────────────────────────────────
export interface LoginResponse {
  token: string;
  username: string;
}

// ── Health ───────────────────────────────────────────────────────────────────
export interface HealthResponse {
  status: string;
  database: string;
  env: string;
}

// ── Jobs ────────────────────────────────────────────────────────────────────
export type JobStatus = 'queued' | 'running' | 'completed' | 'failed' | 'importing' | 'import_failed';
export type Priority  = 'High' | 'Medium' | 'Low';

export interface UploadResponse {
  job_id: string;
  file_name: string;
  file_type: string;
  status:    string;   // "importing"
  message: string;
}

export interface IcpResponse {
  job_id: string;
  status: string;
  tasks_queued: number;
  message: string;
}

export interface JobStatusResponse {
  job_id:             string;
  status:             JobStatus;
  file_name?:         string | null;
  file_type?:         string | null;
  total_rows:         number | null;
  processed_rows:     number;           // ← remove null, backend always returns int
  failed_rows:        number;
  progress_pct:       number;           // ← remove null, backend always returns float
  duplicates_removed?: number | null;
  import_error?:       string | null;
  created_at:         string | null;
  completed_at:       string | null;
  llm_model?:         string | null;    // ← ADD
  embed_model?:       string | null;    // ← ADD
  total_tokens_used?: number | null;    // ← ADD
  prompt_tokens?:     number | null;    // ← ADD
  completion_tokens?: number | null;    // ← ADD
}

export interface JobSummary extends JobStatusResponse {
  file_name?: string | null;
  file_type?: string | null;
}

export interface DashboardStatsResponse {
  total_jobs: number;
  completed_jobs: number;
  active_jobs: number;
  failed_jobs: number;
  total_leads: number;
  high_priority: number;
  medium_priority: number;
  low_priority: number;
  avg_final_score: number;
}

// ── Lead ────────────────────────────────────────────────────────────────────
export interface LeadResult {
  lead_id: string;
  contact_name: string | null;
  company_name: string | null;
  job_title: string | null;
  email_address: string | null;
  final_score: number | null;
  priority: Priority | null;
  reasoning: string | null;
  growth_signals: string | null;
  outreach_intelligence: string | null;
  icp_score: number | null;
  authority_score: number | null;
  data_score: number | null;
  growth_score: number | null;
  contact_score: number | null;
  validation_status: string | null;
  source_urls: string[];
  company_description: string | null;
  technologies_used: string | null;
}

// ── Chat ────────────────────────────────────────────────────────────────────
export interface ChatSource {
  company_name: string;
  website_url: string;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  created_at?: string;
}

export interface ChatResponse {
  answer: string;
  tool_used: string | null;
  sources: ChatSource[];
}
