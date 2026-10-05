import type { QueryClient } from '@tanstack/react-query';
import type { LeadResult } from '@/types';

export async function invalidateLeadWorkspace(client: QueryClient) {
  await Promise.all([
    client.invalidateQueries({ queryKey: ['job-results'] }),
    client.invalidateQueries({ queryKey: ['workspace-leads'] }),
    client.invalidateQueries({ queryKey: ['workspace-reminders'] }),
    client.invalidateQueries({ queryKey: ['dashboard-stats'] }),
  ]);
}

// The API stores UTC; date/time inputs and reminder labels use the user's timezone.
export function localDateTime(value: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

export function reminderDate(value: string | null): string {
  if (!value) return 'Not scheduled';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Date unavailable' : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

export function workspaceRequestId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// Detail loads, tracking saves, and research polling may finish out of order.
// Keep the newest research and versioned workflow fields independently.
export function mergeLeadSnapshot(current: LeadResult | null, incoming: LeadResult): LeadResult {
  if (!current || current.lead_id !== incoming.lead_id) return incoming;
  const checked = (value: string | null) => value ? new Date(value).getTime() || 0 : 0;
  const research = checked(incoming.evidence_checked_at) < checked(current.evidence_checked_at) ? current : incoming;
  const workflow = incoming.tracking_version < current.tracking_version ? current : incoming;
  const completedRun = !!current.refresh_id && current.refresh_id === incoming.refresh_id
    && current.refresh_status === 'completed' && incoming.refresh_status !== 'completed';
  const refresh = completedRun || checked(incoming.refresh_requested_at) < checked(current.refresh_requested_at) ? current : incoming;
  return {
    ...research,
    reviewed: workflow.reviewed, tracking_notes: workflow.tracking_notes, tracking_version: workflow.tracking_version,
    owner_id: workflow.owner_id, owner_name: workflow.owner_name, owner_active: workflow.owner_active,
    follow_up_at: workflow.follow_up_at, follow_up_note: workflow.follow_up_note,
    follow_up_completed_at: workflow.follow_up_completed_at, follow_up_status: workflow.follow_up_status,
    refresh_id: refresh.refresh_id, refresh_status: refresh.refresh_status,
    refresh_error: refresh.refresh_error, refresh_requested_at: refresh.refresh_requested_at,
  };
}
