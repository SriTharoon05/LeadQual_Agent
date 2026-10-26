import { apiClient } from '@/lib/apiClient';
import type { AdminSettings, SettingsSave, EventPage, CostJobs, JobCostDetail, LeadCostDetail } from '@/types/admin';

export const getAdminSettings = async (signal?: AbortSignal): Promise<AdminSettings> =>
  (await apiClient.get('/admin/settings', { signal })).data;
export const saveAdminSettings = async (body: SettingsSave): Promise<AdminSettings> =>
  (await apiClient.put('/admin/settings', body)).data;
export const refreshExchangeRate = async (): Promise<AdminSettings> =>
  (await apiClient.post('/admin/settings/exchange-rate')).data;
export const getOperationalEvents = async (params: { job_id?: string; severity?: string; rate_only?: boolean; before_id?: number; limit?: number }, signal?: AbortSignal): Promise<EventPage> =>
  (await apiClient.get('/admin/logs', { params, signal })).data;
export const getJobEvents = async (jobId: string, signal?: AbortSignal): Promise<EventPage> =>
  (await apiClient.get(`/jobs/${jobId}/events`, { params: { limit: 50 }, signal })).data;
export const getCostJobs = async (offset = 0, search = '', signal?: AbortSignal): Promise<CostJobs> =>
  (await apiClient.get('/admin/cost/jobs', { params: { offset, search, limit: 25 }, signal })).data;
export const getJobCost = async (jobId: string, offset = 0, signal?: AbortSignal): Promise<JobCostDetail> =>
  (await apiClient.get(`/admin/cost/jobs/${jobId}`, { params: { offset, limit: 50 }, signal })).data;
export const getLeadCost = async (jobId: string, leadId: string, offset = 0, signal?: AbortSignal): Promise<LeadCostDetail> =>
  (await apiClient.get(`/admin/cost/jobs/${jobId}/leads/${leadId}`, { params: { offset, limit: 50 }, signal })).data;
