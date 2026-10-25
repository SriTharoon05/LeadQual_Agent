import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { CalendarClock, ChevronLeft, ChevronRight } from 'lucide-react';
import { isAxiosError } from 'axios';
import { getLeadDetail, getReminders, getTeam, updateLeadTracking } from '@/api/endpoints';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useToast } from '@/components/ui/Toast';
import { invalidateLeadWorkspace, mergeLeadSnapshot, reminderDate } from '@/lib/workspace';
import { OwnerBadge } from '@/components/LeadWorkflow';
import type { LeadResult, WorkspaceLead } from '@/types';

const LeadDetailDrawer = lazy(() => import('@/components/LeadDetailDrawer').then((module) => ({ default: module.LeadDetailDrawer })));
const PAGE_SIZE = 10;
type ReminderStatus = 'due' | 'upcoming' | 'completed';

export function FollowUpReminders() {
  const [status, setStatus] = useState<ReminderStatus>('due');
  const [owner, setOwner] = useState('');
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<{ jobId: string; lead: LeadResult } | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const savingRef = useRef(false);
  const opening = useRef(0);
  const detailRequest = useRef<AbortController | null>(null);
  useEffect(() => () => { opening.current++; detailRequest.current?.abort(); }, []);
  const qc = useQueryClient();
  const { toast } = useToast();
  const team = useQuery({ queryKey: ['workspace-team'], queryFn: getTeam });
  const reminders = useQuery({
    queryKey: ['workspace-reminders', status, owner, page],
    queryFn: ({ signal }) => getReminders(status, page * PAGE_SIZE, PAGE_SIZE, owner, signal),
    refetchInterval: 60_000,
  });
  const total = reminders.data?.total || 0;
  const counts = reminders.data?.counts;
  useEffect(() => {
    if (reminders.data) {
      const last = Math.max(0, Math.ceil(reminders.data.total / PAGE_SIZE) - 1);
      if (page > last) setPage(last);
    }
  }, [reminders.data, page]);

  const openLead = async (item: WorkspaceLead) => {
    const request = ++opening.current;
    detailRequest.current?.abort();
    const controller = new AbortController();
    detailRequest.current = controller;
    try {
      const lead = await getLeadDetail(item.job_id, item.lead_id, controller.signal);
      if (request === opening.current) setSelected({ jobId: item.job_id, lead });
    } catch { if (request === opening.current) toast('Could not load this lead', 'error'); }
  };
  const complete = async (item: WorkspaceLead) => {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(item.lead_id);
    try {
      await updateLeadTracking(item.job_id, item.lead_id, { version: item.tracking_version, follow_up_completed: true });
      toast('Follow-up completed', 'success');
      await invalidateLeadWorkspace(qc);
    } catch (cause) {
      toast(isAxiosError(cause) && cause.response?.status === 409 ? 'This lead changed elsewhere. Latest reminders are loaded; review and try again.' : 'Could not complete this follow-up', 'error');
      await reminders.refetch();
    } finally { savingRef.current = false; setSaving(null); }
  };
  return <Card className="p-5 space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="flex items-center gap-2 text-sm font-semibold text-slate-800"><CalendarClock className="h-4 w-4 text-brand-600" />Follow-up reminders</h2><p className="mt-1 text-xs text-slate-500">Saved reminders appear here when you return. This list updates every minute.</p></div>
      <Link to="/leads"><Button size="sm" variant="outline">Open all leads</Button></Link>
    </div>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex flex-wrap gap-1" role="group" aria-label="Reminder status">
        {(['due', 'upcoming', 'completed'] as const).map((value) => <button key={value} onClick={() => { setStatus(value); setPage(0); }} aria-pressed={status === value} className={`rounded-lg px-3 py-2 text-xs font-medium ${status === value ? 'bg-brand-100 text-brand-700' : 'bg-slate-50 text-slate-600 hover:bg-slate-100'}`}>{value === 'due' ? 'Due' : value === 'upcoming' ? 'Upcoming' : 'Completed'} <span className="ml-1">{counts?.[value] ?? '—'}</span></button>)}
      </div>
      <select aria-label="Filter reminders by owner" value={owner} onChange={(event) => { setOwner(event.target.value); setPage(0); }} className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs">
        <option value="">All owners</option>{owner && !team.data?.some((member) => member.id === owner) && <option value={owner}>Selected owner</option>}{team.data?.map((member) => <option key={member.id} value={member.id}>{member.name}{member.active ? '' : ' (inactive)'}</option>)}
      </select>
    </div>
    {team.isError && <p role="alert" className="text-xs text-rose-700">Could not load owner names. <button className="underline" onClick={() => void team.refetch()}>Retry directory</button></p>}
    {reminders.isPending ? <p className="py-5 text-sm text-slate-500">Loading reminders…</p> : reminders.isError ? <p role="alert" className="text-sm text-rose-700">Could not load reminders. <button className="underline" onClick={() => void reminders.refetch()}>Retry</button></p> : !reminders.data?.items.length ? <p className="py-5 text-sm text-slate-500">{page > 0 ? 'No reminders on this page. Return to the previous page.' : `No ${status} follow-ups. Schedule one from a lead's details.`}</p> : <div className="divide-y divide-slate-100">
      {reminders.data.items.map((item) => <div key={item.lead_id} className="flex flex-wrap items-center justify-between gap-3 py-3">
        <button onClick={() => void openLead(item)} className="min-w-0 text-left group">
          <p className="text-sm font-medium text-slate-800 group-hover:text-brand-700">{item.contact_name || 'Unnamed contact'} · {item.company_name || 'Unknown company'}</p>
          <p className={`mt-1 text-xs ${status === 'due' ? 'text-rose-600' : 'text-slate-500'}`}>{reminderDate(item.follow_up_at)}</p>
          {item.follow_up_note && <p className="mt-1 max-w-lg truncate text-xs text-slate-600">{item.follow_up_note}</p>}
          <div className="mt-2"><OwnerBadge lead={item} /></div>
        </button>
        <div className="flex gap-2"><Button size="sm" variant="ghost" onClick={() => void openLead(item)}>Details</Button>{status !== 'completed' && <Button size="sm" variant="outline" disabled={!!saving} loading={saving === item.lead_id} onClick={() => void complete(item)}>Complete</Button>}</div>
      </div>)}
    </div>}
    <div className="flex items-center justify-between text-xs text-slate-500">
      <span>{total.toLocaleString()} {status} follow-ups · Page {page + 1}</span>
      <div className="flex gap-1"><Button size="sm" variant="ghost" disabled={page === 0 || reminders.isFetching} onClick={() => setPage((value) => Math.max(0, value - 1))} aria-label="Previous reminders"><ChevronLeft className="h-4 w-4" /></Button><Button size="sm" variant="ghost" disabled={!reminders.data?.has_more || reminders.isFetching} onClick={() => setPage((value) => value + 1)} aria-label="Next reminders"><ChevronRight className="h-4 w-4" /></Button></div>
    </div>
    {selected && <Suspense fallback={null}><LeadDetailDrawer lead={selected.lead} jobId={selected.jobId} open onClose={() => { opening.current += 1; setSelected(null); }} onSaved={(lead) => setSelected((current) => current?.lead.lead_id === lead.lead_id ? { ...current, lead: mergeLeadSnapshot(current.lead, lead) } : current)} /></Suspense>}
  </Card>;
}
