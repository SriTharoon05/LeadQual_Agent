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
  reused_rows: number;
  research_reused_rows: number;
  icp_prompt?: string | null;
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
  upload_pending?:     boolean;
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
  evidence_age_days: number | null;
  evidence_status: 'fresh' | 'stale' | 'unknown';
  refresh_status: 'queued' | 'running' | 'completed' | 'failed' | null;
  refresh_id: string | null;
  refresh_error: string | null;
  refresh_requested_at: string | null;
  reviewed: boolean;
  tracking_notes: string;
  tracking_version: number;
  reuse_kind: 'full' | 'research' | null;
  reused_from_lead_id: string | null;
  icp_input: string | null;
  icp_proofs: IcpProof[];
  evidence_checked_at: string | null;
  employment_status: 'current' | 'former' | 'unclear';
  lead_id: string;
  row_number: number | null;
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
  growth_score: number | null;
  contact_score: number | null;
  qualification_status: 'qualified' | 'needs_review' | 'disqualified' | 'uncontactable' | null;
  scoring_version: string | null;
  scoring_details: { reasons?: string[]; raw_weighted_score?: number; weights?: { icp: number; contact: number; growth: number } };
  source_urls: string[];
  company_description: string | null;
  technologies_used: string | null;
}

// Shared reviewed state and notes are independent of uploads.
export interface RefreshRun {
  id: string;
  lead_id: string;
  status: 'queued' | 'running' | 'completed' | 'failed';
  requested_by: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
}

export interface TrackingUpdate {
  version: number;
  reviewed?: boolean;
  notes?: string;
}

export interface IcpProof {
  required?: boolean;
  methods?: {
    kind: string; value: string; status: string; delivery_status: string;
    checked_at: string | null; explanation: string;
    observations: { type: string; domain?: string; records?: string[]; response?: string; code?: number; e164?: string; format_valid?: boolean; possible?: boolean }[];
    sources: { url: string; title: string; excerpt: string }[];
  }[];
  field: string;
  label: string;
  value: string;
  status: 'confirmed' | 'unclear' | 'conflicting';
  explanation: string;
  requirement: string;
  verdict: 'match' | 'mismatch' | 'unclear' | 'not_specified';
  reasoning: string;
  checked_at: string | null;
  sources: { url: string; title: string; excerpt: string }[];
}

// ── Chat ────────────────────────────────────────────────────────────────────
export interface ChatSource {
  company_name: string;
  website_url: string;
  title?: string;
  excerpt?: string;
}

export interface ChatSelection {
  kind: 'company' | 'person';
  value: string;
}

export interface ChatClarification {
  prompt: string;
  choices: (ChatSelection & { label: string })[];
  original_message: string;
}

export interface ChatContext {
  company?: string;
  person?: string;
  scope?: { companies?: string[]; person_ids?: string[] };
  query?: Record<string, unknown>;
}

export interface ChatMessage {
  id: string;
  request_id: string | null;
  role: 'user' | 'assistant';
  content: string;
  created_at: string;
  sequence: number;
  sources: ChatSource[];
  tool_used: string | null;
  metadata?: {
    clarification?: ChatClarification | null;
    context?: ChatContext | null;
    degraded?: boolean;
  } | null;
}

export interface ChatRequest {
  message: string;
  request_id: string;
  conversation_id: string;
  selection?: ChatSelection;
}

export interface ChatResponse {
  answer: string;
  tool_used: string | null;
  sources: ChatSource[];
  user_message: ChatMessage;
  assistant_message: ChatMessage;
  degraded: boolean;
  clarification?: ChatClarification | null;
  context?: ChatContext | null;
}
