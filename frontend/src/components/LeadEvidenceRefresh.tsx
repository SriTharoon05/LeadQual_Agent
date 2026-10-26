import { useEffect, useRef, useState } from 'react';
import { isAxiosError } from 'axios';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';
import { getLeadDetail, getLeadRefresh, refreshLead } from '@/api/endpoints';
import { Button } from '@/components/ui/Button';
import { useToast } from '@/components/ui/Toast';
import { invalidateLeadData, formatEvidenceDate, newRequestId } from '@/lib/leadCache';
import type { LeadResult } from '@/types';

export function EvidenceBadge({ lead }: { lead: LeadResult }) {
  const status = lead.evidence_status || 'unknown';
  return <span title={status === 'unknown' ? 'Evidence has no reliable research date' : `Checked ${lead.evidence_age_days ?? 0} days ago. Fresh evidence is at most 90 days old.`}
    className={`rounded-full px-2 py-0.5 text-xs ${status === 'fresh' ? 'bg-emerald-50 text-emerald-700' : status === 'stale' ? 'bg-amber-50 text-amber-800' : 'bg-slate-100 text-slate-500'}`}>
    {status === 'fresh' ? 'Fresh evidence' : status === 'stale' ? 'Stale evidence' : 'Evidence date unknown'}
  </span>;
}

export function LeadEvidenceRefresh({ lead, jobId, onSaved }: { lead: LeadResult; jobId: string; onSaved: (lead: LeadResult) => void }) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
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
        await invalidateLeadData(qc);
      } catch { if (!cancelled) setError('Research finished, but the updated details could not be loaded. Retry loading the status.'); }
    };
    void reload();
    return () => { cancelled = true; };
  }, [latest, jobId, lead.lead_id, qc]);

  const start = async () => {
    if (sendingRef.current || busy) return;
    sendingRef.current = true;
    setSending(true); setError('');
    requestId.current ||= newRequestId();
    try { sessionStorage.setItem(requestKey, requestId.current); } catch { /* storage can be disabled */ }
    try {
      await refreshLead(jobId, lead.lead_id, requestId.current);
      clearRequestId();
      toast('Evidence refresh queued', 'success');
      await refresh.refetch();
      await invalidateLeadData(qc);
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
    } finally { sendingRef.current = false; setSending(false); }
  };
  return <div className="mb-4 rounded-xl border border-slate-200 bg-white p-4 space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <EvidenceBadge lead={lead} />
      <Button size="sm" variant="outline" icon={<RefreshCw className={`h-3.5 w-3.5 ${busy ? 'animate-spin' : ''}`} />} loading={sending} disabled={busy || refresh.isPending || refresh.isError} onClick={() => void start()}>
        {busy ? `Refresh ${active?.status || lead.refresh_status}` : requestId.current ? 'Retry refresh request' : 'Refresh evidence'}
      </Button>
    </div>
    <p className="text-xs text-slate-500">Last checked: {lead.evidence_checked_at ? formatEvidenceDate(lead.evidence_checked_at) : 'Unknown'}. Fresh evidence is at most 90 days old.</p>
    <p className="text-xs text-slate-500">Refresh runs new AI research and may use credits. Current results remain available while it runs.</p>
    {busy && <p role="status" className="text-xs text-brand-700">{active?.status === 'running' ? 'New research is running.' : 'Waiting for a research worker.'} You can leave this page and return later.{active?.error_message ? ` ${active.error_message}` : ''}</p>}
    {!busy && latest?.status === 'completed' && <p className="text-xs text-emerald-700">Research completed {formatEvidenceDate(latest.finished_at)}.</p>}
    {!busy && latest?.status === 'failed' && <p role="alert" className="text-xs text-rose-700">Refresh failed: {latest.error_message || 'Please try again.'}</p>}
    {(error || refresh.isError) && <p role="alert" className="text-xs text-rose-700">{error || 'Could not load refresh status.'} <button className="underline" onClick={() => {
      setError('');
      void (async () => {
        await refresh.refetch();
        try { callback.current(await getLeadDetail(jobId, lead.lead_id)); await invalidateLeadData(qc); }
        catch { setError('Could not reload the updated lead. Please try again.'); }
      })();
    }}>Reload status</button></p>}
  </div>;
}
