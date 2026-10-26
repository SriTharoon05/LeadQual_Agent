export interface Pricing {
  model: string; embedding_model: string; input_per_million: number; cached_per_million: number;
  output_per_million: number; embedding_per_million: number; search_per_thousand: number;
  confirmed: boolean; basis: string; token_source: string; search_source: string;
}
export interface AdminSettings {
  concurrency: number; version: number; pricing: Pricing; usd_inr: number | null;
  fx_as_of: string | null; fx_source: string | null; updated_by: string | null; updated_at: string | null;
  active: number | null; workers: { hostname: string; pool: string; capacity: number }[];
  redis_available: boolean; telemetry_pending: number | null; deployment: string; embedding_deployment: string;
  cooldown_seconds: number;
}
export interface SettingsSave {
  concurrency: number; version: number; usd_inr?: number | null;
  pricing?: Pick<Pricing, 'input_per_million' | 'cached_per_million' | 'output_per_million' | 'embedding_per_million' | 'search_per_thousand' | 'confirmed'>;
}
export interface OperationalEvent {
  id?: number | null; event_id: string; job_id: string | null; lead_id: string | null;
  severity: 'WARNING' | 'ERROR'; kind: string; stage: string | null; component: string | null;
  message: string; retry_after?: number | null; occurred_at: string;
  file_name?: string | null; company_name?: string | null; contact_name?: string | null;
}
export interface EventPage {
  items: OperationalEvent[]; has_more: boolean; before_id: number | null;
  source: 'sql' | 'buffered'; warning?: string;
  runtime?: { concurrency: number | null; active: number | null; cooldown_seconds: number };
}
export interface CostMetrics {
  calls: number; input_tokens: number; cached_tokens: number; output_tokens: number;
  search_requests: number; tool_items: number; missing_search_usage: number; incomplete_calls: number;
  legacy_calls: number; failed_or_unreported_calls: number; input_usd: number; cached_usd: number;
  unpriced_calls: number;
  output_usd: number; search_usd: number; total_usd: number; total_inr: number | null;
}
export interface StageCost extends CostMetrics { stage: string | null; module: string | null }
export interface JobCost extends CostMetrics { job_id: string; file_name: string | null; status: string; total_rows: number | null; created_at: string }
export interface CostJobs { items: JobCost[]; total: number; pricing: Pricing; usd_inr: number | null; fx_as_of: string | null; fx_source: string | null; estimated: boolean; unallocated: CostMetrics }
export interface LeadCost extends CostMetrics { lead_id: string; row_index: number | null; company_name: string | null; contact_name: string | null }
export interface JobCostDetail {
  job_id: string; file_name: string | null; summary: CostMetrics; by_stage: StageCost[];
  job_level: CostMetrics; leads: LeadCost[]; total_leads: number; pricing: Pricing; usd_inr: number | null; fx_as_of: string | null;
}
export interface CallCost {
  id: string; stage: string | null; module: string | null; model: string | null; billing_model: string | null;
  prompt_tokens: number | null; cached_prompt_tokens: number | null; completion_tokens: number | null;
  web_search_calls: number | null; search_requests: number | null; status: string | null; error_code: string | null;
  duration_ms: number | null; provider_request_id: string | null; cost_complete: boolean | null; created_at: string;
  cost_input_usd: number | null; cost_cached_usd: number | null; cost_output_usd: number | null;
  cost_search_usd: number | null; cost_total_usd: number | null; pricing_snapshot: Record<string, unknown> | null;
}
export interface LeadCostDetail { lead_id: string; company_name: string | null; contact_name: string | null; summary: CostMetrics; by_stage: StageCost[]; calls: CallCost[]; total_calls: number; usd_inr: number | null }
