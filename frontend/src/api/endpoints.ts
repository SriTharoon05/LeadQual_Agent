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
  DashboardStatsResponse,
} from '@/types';

// ── Auth ────────────────────────────────────────────────────────────────────
export const login = async (username: string, password: string): Promise<LoginResponse> => {
  const { data } = await apiClient.post<LoginResponse>('/auth/login', { username, password });
  return data;
};

// ── Health ───────────────────────────────────────────────────────────────────
export const getHealth = async (): Promise<HealthResponse> => {
  const { data } = await apiClient.get<HealthResponse>('/health');
  return data;
};

// ── Jobs / Upload pipeline ──────────────────────────────────────────────────
export const uploadFile = async (file: File): Promise<UploadResponse> => {
  const form = new FormData();
  form.append('file', file);
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

export const getJobStatus = async (jobId: string): Promise<JobStatusResponse> => {
  const { data } = await apiClient.get<JobStatusResponse>(`/jobs/${jobId}/status`);
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
): Promise<PaginatedLeads> => {
  const { data } = await apiClient.get(`/jobs/${jobId}/results`, {
    params: {
      limit, offset,
      search: filters?.search || undefined,
      priority: filters?.priority === 'all' ? undefined : filters?.priority,
      sort_by: filters?.sortBy,
      sort_dir: filters?.sortDir,
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

export const getLeadDetail = async (jobId: string, leadId: string): Promise<LeadResult> => {
  const { data } = await apiClient.get<LeadResult>(`/jobs/${jobId}/results/${leadId}`);
  return data;
};

export const updateLeadTracking = async (
  jobId: string, leadId: string,
  changes: { version: number; reviewed?: boolean; notes?: string },
): Promise<LeadResult> => {
  const { data } = await apiClient.patch<LeadResult>(`/jobs/${jobId}/results/${leadId}/tracking`, changes);
  return data;
};

export const deleteJob = async (jobId: string): Promise<void> => {
  await apiClient.delete(`/jobs/${jobId}`);
};

export const exportExcel = (jobId: string): string =>
  `${apiClient.defaults.baseURL}/jobs/${jobId}/export/excel`;

export const exportCsv = (jobId: string): string =>
  `${apiClient.defaults.baseURL}/jobs/${jobId}/export/csv`;

// Download export with auth header
export const downloadExport = async (jobId: string, format: 'excel' | 'csv'): Promise<void> => {
  const ext = format === 'excel' ? 'excel' : 'csv';
  const resp = await apiClient.get(`/jobs/${jobId}/export/${ext}`, {
    responseType: 'blob',
  });
  const disposition = resp.headers['content-disposition'] ?? '';
  const match = disposition.match(/filename="?([^";\n]+)"?/);
  const filename = match?.[1] ?? `job_${jobId}.${format === 'excel' ? 'xlsx' : 'csv'}`;
  const url = window.URL.createObjectURL(resp.data);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  window.URL.revokeObjectURL(url);
};

// ── Chat ────────────────────────────────────────────────────────────────────
export const sendChat = async (jobId: string, message: string): Promise<ChatResponse> => {
  const { data } = await apiClient.post<ChatResponse>(`/jobs/${jobId}/chat`, { message });
  return data;
};

export const getChatHistory = async (jobId: string): Promise<ChatMessage[]> => {
  const { data } = await apiClient.get<ChatMessage[]>(`/jobs/${jobId}/chat/history`);
  return data ?? [];
};
