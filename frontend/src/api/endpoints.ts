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
  TeamMember, SavedSegment, LeadFilters, WorkspaceLeadPage, ReminderPage,
  RefreshRun, TrackingUpdate,
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
  changes: TrackingUpdate,
): Promise<LeadResult> => {
  const { data } = await apiClient.patch<LeadResult>(`/jobs/${jobId}/results/${leadId}/tracking`, changes);
  return data;
};

// ── Shared team workspace ──────────────────────────────────────────────────
export const getTeam = async (): Promise<TeamMember[]> => {
  const { data } = await apiClient.get<TeamMember[]>('/workspace/team', { params: { include_inactive: true } });
  return data;
};

export const createTeamMember = async (values: { name: string; email: string | null }): Promise<TeamMember> => {
  const { data } = await apiClient.post<TeamMember>('/workspace/team', values);
  return data;
};

export const updateTeamMember = async (id: string, changes: { version: number; name?: string; email?: string | null; active?: boolean }): Promise<TeamMember> => {
  const { data } = await apiClient.patch<TeamMember>(`/workspace/team/${id}`, changes);
  return data;
};

export const getSegments = async (): Promise<SavedSegment[]> => {
  const { data } = await apiClient.get<SavedSegment[]>('/workspace/segments');
  return data;
};

export const createSegment = async (name: string, filters: LeadFilters): Promise<SavedSegment> => {
  const { data } = await apiClient.post<SavedSegment>('/workspace/segments', { name, filters });
  return data;
};

export const updateSegment = async (id: string, version: number, name: string, filters: LeadFilters): Promise<SavedSegment> => {
  const { data } = await apiClient.patch<SavedSegment>(`/workspace/segments/${id}`, { version, name, filters });
  return data;
};

export const deleteSegment = async (id: string, version: number): Promise<void> => {
  await apiClient.delete(`/workspace/segments/${id}`, { params: { version } });
};

export const getWorkspaceLeads = async (filters: LeadFilters, offset = 0, limit = 50, segmentId?: string): Promise<WorkspaceLeadPage> => {
  const { data } = await apiClient.get<WorkspaceLeadPage>('/workspace/leads', {
    params: { ...(segmentId ? { segment_id: segmentId } : filters), offset, limit },
  });
  return data;
};

export const getReminders = async (status: 'all' | 'due' | 'upcoming' | 'completed', offset = 0, limit = 10, ownerId?: string): Promise<ReminderPage> => {
  const { data } = await apiClient.get<ReminderPage & { due_count?: number; upcoming_count?: number; completed_count?: number }>('/workspace/reminders', {
    params: { status, offset, limit, owner_id: ownerId || undefined },
  });
  return { ...data, counts: data.counts || { due: data.due_count || 0, upcoming: data.upcoming_count || 0, completed: data.completed_count || 0 } };
};

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
  jobId: string, conversationId: string, signal?: AbortSignal,
): Promise<ChatMessage[]> => {
  const { data } = await apiClient.get<ChatMessage[]>(`/jobs/${jobId}/chat/history`, {
    params: { conversation_id: conversationId },
    signal,
  });
  if (!Array.isArray(data) || data.some((message) => !message || typeof message.id !== 'string'
      || !['user', 'assistant'].includes(message.role) || !Number.isInteger(message.sequence))) {
    throw new Error('The server returned invalid chat history. Please reload the conversation.');
  }
  return data;
};
