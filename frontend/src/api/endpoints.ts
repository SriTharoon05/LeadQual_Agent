import { apiClient } from '@/lib/apiClient';
import type {
  LoginResponse,
  HealthResponse,
  UploadResponse,
  IcpResponse,
  JobStatusResponse,
  JobSummary,
  LeadResult,
  ChatResponse,
  ChatMessage,
  ChatRequest,
  DashboardStatsResponse,
  RefreshRun, TrackingUpdate,
} from '@/types';

// ── Auth ────────────────────────────────────────────────────────────────────
export const login = async (username: string, password: string, signal?: AbortSignal): Promise<LoginResponse> => {
  const { data } = await apiClient.post<LoginResponse>('/auth/login', { username, password }, { signal });
  return data;
};

// ── Health ───────────────────────────────────────────────────────────────────
export const getHealth = async (): Promise<HealthResponse> => {
  const { data } = await apiClient.get<HealthResponse>('/health');
  return data;
};

// ── Jobs / Upload pipeline ──────────────────────────────────────────────────
export const uploadFile = async (file: File, requestId?: string): Promise<UploadResponse> => {
  const form = new FormData();
  form.append('file', file);
  if (requestId) form.append('request_id', requestId);
  const { data } = await apiClient.post<UploadResponse>('/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 120000,   // headroom for the initial file transfer of large files
  });
  return data;         // { job_id, status: "importing", ... }
};


export const startIcp = async (jobId: string, icpPrompt: string): Promise<IcpResponse> => {
  const { data } = await apiClient.post<IcpResponse>(`/jobs/${jobId}/icp`, { icp_prompt: icpPrompt });
  return data;
};

export const getJobs = async (): Promise<JobSummary[]> => {
  const { data } = await apiClient.get<JobSummary[]>('/jobs');
  return data ?? [];
};

export const getDashboardStats = async (): Promise<DashboardStatsResponse> => {
  const { data } = await apiClient.get<DashboardStatsResponse>('/dashboard/stats');
  return data;
};

export const getJobStatus = async (jobId: string, signal?: AbortSignal): Promise<JobStatusResponse> => {
  const { data } = await apiClient.get<JobStatusResponse>(`/jobs/${jobId}/status`, { signal });
  return data;
};

// ── Results (paginated) ──────────────────────────────────────────────────────
// Backend returns: { items, total, limit, offset, returned, has_more }
export interface PaginatedLeads {
  items:    LeadResult[];
  total:    number;   // total leads for the whole job
  limit:    number;   // page size used
  offset:   number;   // starting index of this page
  returned: number;   // rows in this page
  has_more: boolean;  // whether more pages exist after this one
}

/**
 * Fetch ONE page of results. Used by the results table for windowed,
 * server-side pagination (only loads what the user is viewing).
 */
export const getResultsPage = async (
  jobId: string,
  offset = 0,
  limit = 50,
  filters?: { search?: string; priority?: string; sortBy?: string; sortDir?: string },
  signal?: AbortSignal,
): Promise<PaginatedLeads> => {
  const { data } = await apiClient.get(`/jobs/${jobId}/results`, {
    signal,
    params: {
      limit, offset,
      search: filters?.search || undefined,
      priority: filters?.priority === 'all' ? undefined : filters?.priority,
      sort_by: filters?.sortBy,
      sort_dir: filters?.sortDir,
      details: false,
    },
  });
  return {
    items:    data?.items ?? [],
    total:    data?.total ?? 0,
    limit:    data?.limit ?? limit,
    offset:   data?.offset ?? offset,
    returned: data?.returned ?? (data?.items?.length ?? 0),
    has_more: data?.has_more ?? false,
  };
};

/**
 * Fetch ALL results by looping through every page.
 * Use ONLY where the full set is genuinely needed (e.g. dashboard KPIs) —
 * NOT for the table, which should use getResultsPage for windowed loading.
 */
export const getJobResults = async (jobId: string): Promise<LeadResult[]> => {
  const all: LeadResult[] = [];
  let offset = 0;
  const limit = 200;
  // Safety cap so a bad has_more can never infinite-loop.
  for (let guard = 0; guard < 1000; guard++) {
    const page = await getResultsPage(jobId, offset, limit);
    all.push(...page.items);
    if (!page.has_more || page.items.length === 0) break;
    offset += limit;
  }
  return all;
};

export const getLeadDetail = async (jobId: string, leadId: string, signal?: AbortSignal): Promise<LeadResult> => {
  const { data } = await apiClient.get<LeadResult>(`/jobs/${jobId}/results/${leadId}`, { signal });
  return data;
};

export const updateLeadTracking = async (
  jobId: string, leadId: string,
  changes: TrackingUpdate,
): Promise<LeadResult> => {
  const { data } = await apiClient.patch<LeadResult>(`/jobs/${jobId}/results/${leadId}/tracking`, changes);
  return data;
};

// ── Evidence refresh ──────────────────────────────────────────────────
export const refreshLead = async (jobId: string, leadId: string, requestId: string): Promise<RefreshRun> => {
  const { data } = await apiClient.post<RefreshRun>(`/jobs/${jobId}/results/${leadId}/refresh`, { request_id: requestId });
  return data;
};

export const getLeadRefresh = async (jobId: string, leadId: string): Promise<{ active: RefreshRun | null; latest: RefreshRun | null }> => {
  const { data } = await apiClient.get<{ active: RefreshRun | null; latest: RefreshRun | null }>(`/jobs/${jobId}/results/${leadId}/refresh`);
  return data;
};

export const deleteJob = async (jobId: string): Promise<void> => {
  await apiClient.delete(`/jobs/${jobId}`);
};

export interface ExportStatus {
  export_id: string;
  job_id: string;
  format: 'excel' | 'csv';
  status: 'queued' | 'running' | 'ready' | 'failed';
  phase: string;
  rows_written: number;
  total_rows: number;
  created_at: number;
  ready_at?: number | null;
  snapshot_at?: string | null;
  error?: string | null;
  size_bytes?: number | null;
  filename?: string | null;
  download_path?: string;
}

export const prepareExport = async (jobId: string, format: 'excel' | 'csv', signal?: AbortSignal): Promise<ExportStatus> => {
  const { data } = await apiClient.post<ExportStatus>(`/jobs/${jobId}/exports`, null, {
    params: { format }, timeout: 20_000, signal,
  });
  return data;
};

export const getExportStatus = async (jobId: string, exportId: string, signal?: AbortSignal): Promise<ExportStatus> => {
  const { data } = await apiClient.get<ExportStatus>(`/jobs/${jobId}/exports/${exportId}`, {
    timeout: 20_000, signal,
  });
  return data;
};

// A native browser transfer avoids buffering the entire file as a JS Blob.
export const exportDownloadUrl = (path: string): string => {
  if (!/^\/jobs\/[0-9a-f-]+\/exports\/[0-9a-f-]+\/file\?ticket=/i.test(path)) {
    throw new Error('The server returned an invalid download link.');
  }
  return new URL(`${(apiClient.defaults.baseURL ?? '').replace(/\/$/, '')}${path}`, window.location.origin).href;
};

// ── Chat ────────────────────────────────────────────────────────────────────
export const sendChat = async (
  jobId: string, request: ChatRequest, signal?: AbortSignal,
): Promise<ChatResponse> => {
  const { data } = await apiClient.post<ChatResponse>(`/jobs/${jobId}/chat`, request, {
    timeout: 120_000,
    signal,
  });
  return data;
};

export const getChatHistory = async (
  jobId: string, conversationId: string, signal?: AbortSignal, beforeSequence?: number, limit = 100,
): Promise<ChatMessage[]> => {
  const { data } = await apiClient.get<ChatMessage[]>(`/jobs/${jobId}/chat/history`, {
    params: { conversation_id: conversationId, before_sequence: beforeSequence, limit },
    signal,
  });
  if (!Array.isArray(data) || data.some((message) => !message || typeof message.id !== 'string'
      || typeof message.content !== 'string' || typeof message.created_at !== 'string'
      || !Number.isFinite(Date.parse(message.created_at))
      || (message.request_id != null && typeof message.request_id !== 'string')
      || !['user', 'assistant'].includes(message.role) || !Number.isInteger(message.sequence)
      || message.sequence < 1 || (message.sources != null && !Array.isArray(message.sources)))) {
    throw new Error('The server returned invalid chat history. Please reload the conversation.');
  }
  return data.map((message) => ({ ...message, sources: message.sources ?? [] }));
};
