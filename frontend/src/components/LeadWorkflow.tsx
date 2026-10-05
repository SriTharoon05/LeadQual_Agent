import { useEffect, useRef, useState } from 'react';
import { isAxiosError } from 'axios';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { CalendarClock, RefreshCw, UserRound } from 'lucide-react';
import { getLeadDetail, getLeadRefresh, getTeam, refreshLead, updateLeadTracking } from '@/api/endpoints';
import { Button } from '@/components/ui/Button';
import { Input, Label, Textarea } from '@/components/ui/Input';
import { useToast } from '@/components/ui/Toast';
import { invalidateLeadWorkspace, localDateTime, reminderDate, workspaceRequestId } from '@/lib/workspace';
import type { LeadResult, TrackingUpdate } from '@/types';

export function OwnerBadge({ lead }: { lead: LeadResult }) {
  return <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${lead.owner_id ? 'bg-violet-50 text-violet-700' : 'bg-slate-100 text-slate-500'}`}>
    <UserRound className="h-3 w-3" />{lead.owner_name || 'Unassigned'}{lead.owner_id && lead.owner_active === false ? ' · inactive' : ''}
  </span>;
}

export function FollowUpBadge({ lead }: { lead: LeadResult }) {
  const status = lead.follow_up_status || 'unscheduled';
  if (status === 'unscheduled') return <span className="text-xs text-slate-400">No follow-up</span>;
  return <span title={reminderDate(lead.follow_up_at)} className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${status === 'due' ? 'bg-rose-50 text-rose-700' : status === 'completed' ? 'bg-emerald-50 text-emerald-700' : 'bg-sky-50 text-sky-700'}`}>
    <CalendarClock className="h-3 w-3" />{status === 'due' ? 'Due' : status === 'completed' ? 'Completed' : 'Upcoming'}
  </span>;
}

export function EvidenceBadge({ lead }: { lead: LeadResult }) {
  const status = lead.evidence_status || 'unknown';
  return <span title={status === 'unknown' ? 'Evidence has no reliable research date' : `Checked ${lead.evidence_age_days ?? 0} days ago. Fresh evidence is at most 90 days old.`}
    className={`rounded-full px-2 py-0.5 text-xs ${status === 'fresh' ? 'bg-emerald-50 text-emerald-700' : status === 'stale' ? 'bg-amber-50 text-amber-800' : 'bg-slate-100 text-slate-500'}`}>
    {status === 'fresh' ? 'Fresh evidence' : status === 'stale' ? 'Stale evidence' : 'Evidence date unknown'}
  </span>;
}

export function LeadWorkflow({ lead, jobId, onSaved }: { lead: LeadResult; jobId: string; onSaved: (lead: LeadResult) => void }) {
  const [owner, setOwner] = useState(lead.owner_id || '');
  const [date, setDate] = useState(localDateTime(lead.follow_up_at));
  const [note, setNote] = useState(lead.follow_up_note || '');
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const qc = useQueryClient();
  const { toast } = useToast();
  const team = useQuery({ queryKey: ['workspace-team'], queryFn: getTeam });
  useEffect(() => {
    if (!dirty) { setOwner(lead.owner_id || ''); setDate(localDateTime(lead.follow_up_at)); setNote(lead.follow_up_note || ''); }
  }, [lead.owner_id, lead.follow_up_at, lead.follow_up_note, dirty]);

  const save = async (changes: Omit<TrackingUpdate, 'version'>, clearDraft = false) => {
    if (saving) return;
    setSaving(true); setError('');
    try {
      const fresh = await updateLeadTracking(jobId, lead.lead_id, { version: lead.tracking_version, ...changes });
      onSaved(fresh);
      if (clearDraft) setDirty(false);
      toast('Lead workflow saved', 'success');
      await invalidateLeadWorkspace(qc);
    } catch (cause) {
      const conflict = isAxiosError(cause) && cause.response?.status === 409;
      const detail = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
      setError(conflict ? 'This lead changed elsewhere. Your draft is retained; loading the latest saved details…' : typeof detail === 'string' ? detail : 'Could not save this lead. Your draft is retained; please try again.');
      if (conflict) {
        try {
          onSaved(await getLeadDetail(jobId, lead.lead_id));
          setError('Latest saved details are loaded; your draft is retained. Review it and save again.');
        } catch { setError('Your draft is retained, but the latest saved details could not be loaded. Reload the lead before saving again.'); }
        await invalidateLeadWorkspace(qc);
      }
    } finally { setSaving(false); }
  };

  const saveDraft = () => {
    const parsed = date ? new Date(date) : null;
    if (parsed && Number.isNaN(parsed.getTime())) { setError('Choose a valid follow-up date and time.'); return; }
    const dateChanged = date !== localDateTime(lead.follow_up_at);
    const changes = {
      ...(owner !== (lead.owner_id || '') ? { owner_id: owner || null } : {}),
      ...(note !== (lead.follow_up_note || '') ? { follow_up_note: note } : {}),
      ...(dateChanged ? { follow_up_at: parsed?.toISOString() || null } : {}),
    };
    if (!Object.keys(changes).length) { setDirty(false); return; }
    void save(changes, true);
  };

  const snooze = (days: number) => {
    const next = new Date(); next.setDate(next.getDate() + days);
    void save({ follow_up_at: next.toISOString(), follow_up_completed: false });
  };

  const choices = (team.data || []).filter((member) => member.active || member.id === owner);
  return <div className="space-y-4">
    <p className="text-xs text-slate-500">Owner and follow-up are shared across uploads of this lead.</p>
    <div className="flex flex-wrap items-center gap-2"><OwnerBadge lead={lead} /><FollowUpBadge lead={lead} /></div>
    {lead.follow_up_at && <p className="text-xs text-slate-500">Saved follow-up: {reminderDate(lead.follow_up_at)}{lead.follow_up_completed_at ? ` · Completed ${reminderDate(lead.follow_up_completed_at)}` : ''}</p>}
    <div className="grid gap-4 sm:grid-cols-2">
      <div>
        <Label htmlFor={`owner-${lead.lead_id}`}>Lead owner</Label>
        <select id={`owner-${lead.lead_id}`} value={owner} disabled={saving || team.isPending || team.isError} onChange={(event) => { setOwner(event.target.value); setDirty(true); }}
          className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm">
          <option value="">Unassigned</option>
          {owner && !choices.some((member) => member.id === owner) && <option value={owner}>{lead.owner_name || 'Current owner'}</option>}
          {choices.map((member) => <option key={member.id} value={member.id}>{member.name}{!member.active ? ' (inactive)' : ''}</option>)}
        </select>
        {team.isError && <p role="alert" className="mt-1 text-xs text-rose-700">Could not load team members. <button onClick={() => void team.refetch()} className="underline">Retry</button></p>}
      </div>
      <div>
        <Label htmlFor={`follow-date-${lead.lead_id}`}>Follow-up date and time</Label>
        <Input id={`follow-date-${lead.lead_id}`} type="datetime-local" value={date} disabled={saving} onChange={(event) => { setDate(event.target.value); setDirty(true); }} />
        <p className="mt-1 text-xs text-slate-400">Your local timezone · {Intl.DateTimeFormat().resolvedOptions().timeZone}</p>
      </div>
    </div>
    <div>
      <Label htmlFor={`follow-note-${lead.lead_id}`}>Follow-up note</Label>
      <Textarea id={`follow-note-${lead.lead_id}`} value={note} maxLength={10000} rows={3} disabled={saving} placeholder="What should happen next?" onChange={(event) => { setNote(event.target.value); setDirty(true); }} />
    </div>
    <div className="flex flex-wrap gap-2">
      <Button size="sm" variant="secondary" loading={saving} disabled={!dirty} onClick={saveDraft}>Save owner / follow-up</Button>
      {dirty && <Button size="sm" variant="ghost" disabled={saving} onClick={() => { setDirty(false); setError(''); }}>Discard draft</Button>}
      {lead.follow_up_at && <>
        <Button size="sm" variant="outline" disabled={saving || dirty} onClick={() => void save({ follow_up_completed: !lead.follow_up_completed_at })}>{lead.follow_up_completed_at ? 'Reopen follow-up' : 'Mark complete'}</Button>
        {!lead.follow_up_completed_at && <>
          <Button size="sm" variant="ghost" disabled={saving || dirty} onClick={() => snooze(1)}>Snooze 1 day</Button>
          <Button size="sm" variant="ghost" disabled={saving || dirty} onClick={() => snooze(7)}>Snooze 1 week</Button>
        </>}
        <Button size="sm" variant="ghost" disabled={saving || dirty} onClick={() => void save({ follow_up_at: null, follow_up_completed: false })}>Cancel follow-up</Button>
      </>}
    </div>
    {error && <p role="alert" className="text-sm text-rose-700">{error} <button disabled={saving} className="underline" onClick={() => {
      void (async () => {
        try { onSaved(await getLeadDetail(jobId, lead.lead_id)); setError('Latest saved details loaded. Your draft is retained; review it before saving.'); }
        catch { setError('Could not reload this lead. Your draft is retained; please try again.'); }
      })();
    }}>Reload saved details</button></p>}
  </div>;
}

export function LeadEvidenceRefresh({ lead, jobId, onSaved }: { lead: LeadResult; jobId: string; onSaved: (lead: LeadResult) => void }) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const requestKey = `lead-refresh-request:${jobId}:${lead.lead_id}`;
  const requestId = useRef<string | null>((() => {
    try { return sessionStorage.getItem(requestKey); } catch { return null; }
  })());
  const handledRun = useRef<string | null>(null);
  const callback = useRef(onSaved);
  callback.current = onSaved;
  const refresh = useQuery({
    queryKey: ['lead-refresh', jobId, lead.lead_id],
    queryFn: () => getLeadRefresh(jobId, lead.lead_id),
    refetchInterval: (query) => query.state.data?.active || lead.refresh_status === 'queued' || lead.refresh_status === 'running' ? 5000 : false,
  });
  const active = refresh.data?.active;
  const latest = refresh.data?.latest;
  const busy = !!active || (refresh.data === undefined && (lead.refresh_status === 'queued' || lead.refresh_status === 'running'));
  const clearRequestId = () => {
    requestId.current = null;
    try { sessionStorage.removeItem(requestKey); } catch { /* request remains safe within this component */ }
  };

  useEffect(() => {
    const completionKey = latest ? `${latest.id}:${latest.status}:${latest.finished_at || ''}` : '';
    if (!latest || !['completed', 'failed'].includes(latest.status) || handledRun.current === completionKey) return;
    let cancelled = false;
    // Mark handled only after reloading succeeds, so a transient detail failure is recoverable.
    const reload = async () => {
      try {
        const fresh = await getLeadDetail(jobId, lead.lead_id);
        if (cancelled) return;
        handledRun.current = completionKey;
        callback.current(fresh);
        await invalidateLeadWorkspace(qc);
      } catch { if (!cancelled) setError('Research finished, but the updated details could not be loaded. Retry loading the status.'); }
    };
    void reload();
    return () => { cancelled = true; };
  }, [latest, jobId, lead.lead_id, qc]);

  const start = async () => {
    if (sending || busy) return;
    setSending(true); setError('');
    requestId.current ||= workspaceRequestId();
    try { sessionStorage.setItem(requestKey, requestId.current); } catch { /* storage can be disabled */ }
    try {
      await refreshLead(jobId, lead.lead_id, requestId.current);
      clearRequestId();
      toast('Evidence refresh queued', 'success');
      await refresh.refetch();
      await invalidateLeadWorkspace(qc);
    } catch (cause) {
      const status = isAxiosError(cause) ? cause.response?.status : undefined;
      if (status === 409) {
        clearRequestId();
        try {
          const current = await getLeadRefresh(jobId, lead.lead_id);
          qc.setQueryData(['lead-refresh', jobId, lead.lead_id], current);
          if (current.active) {
            setError('');
            toast('A refresh is already queued or running for this lead', 'info');
          } else {
            const detail = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
            setError(typeof detail === 'string' ? detail : 'The lead cannot be refreshed right now. Reload its status and try again.');
          }
          return;
        } catch { setError('Cannot confirm the refresh status. Reload status before trying again.'); return; }
      }
      if (status && status < 500 && status !== 408 && status !== 429) clearRequestId();
      setError('Could not confirm the refresh request. Retry safely; the same request will not start duplicate research.');
      void refresh.refetch();
    } finally { setSending(false); }
  };
  return <div className="mb-4 rounded-xl border border-slate-200 bg-white p-4 space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <EvidenceBadge lead={lead} />
      <Button size="sm" variant="outline" icon={<RefreshCw className={`h-3.5 w-3.5 ${busy ? 'animate-spin' : ''}`} />} loading={sending} disabled={busy || refresh.isPending || refresh.isError} onClick={() => void start()}>
        {busy ? `Refresh ${active?.status || lead.refresh_status}` : requestId.current ? 'Retry refresh request' : 'Refresh evidence'}
      </Button>
    </div>
    <p className="text-xs text-slate-500">Last checked: {lead.evidence_checked_at ? reminderDate(lead.evidence_checked_at) : 'Unknown'}. Fresh evidence is at most 90 days old.</p>
    <p className="text-xs text-slate-500">Refresh runs new AI research and may use credits. Current results remain available while it runs.</p>
    {busy && <p role="status" className="text-xs text-brand-700">{active?.status === 'running' ? 'New research is running.' : 'Waiting for a research worker.'} You can leave this page and return later.{active?.error_message ? ` ${active.error_message}` : ''}</p>}
    {!busy && latest?.status === 'completed' && <p className="text-xs text-emerald-700">Research completed {reminderDate(latest.finished_at)}.</p>}
    {!busy && latest?.status === 'failed' && <p role="alert" className="text-xs text-rose-700">Refresh failed: {latest.error_message || 'Please try again.'}</p>}
    {(error || refresh.isError) && <p role="alert" className="text-xs text-rose-700">{error || 'Could not load refresh status.'} <button className="underline" onClick={() => {
      setError('');
      void (async () => {
        await refresh.refetch();
        try { callback.current(await getLeadDetail(jobId, lead.lead_id)); await invalidateLeadWorkspace(qc); }
        catch { setError('Could not reload the updated lead. Please try again.'); }
      })();
    }}>Reload status</button></p>}
  </div>;
}
