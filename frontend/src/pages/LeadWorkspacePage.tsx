import { lazy, Suspense, useDeferredValue, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { isAxiosError } from 'axios';
import { Bookmark, ChevronLeft, ChevronRight, Filter, Plus, Search, Trash2, Users } from 'lucide-react';
import { createSegment, deleteSegment, getLeadDetail, getSegments, getTeam, getWorkspaceLeads, updateSegment } from '@/api/endpoints';
import { TopBar } from '@/components/TopBar';
import { LeadTracking } from '@/components/LeadTracking';
import { EvidenceBadge, FollowUpBadge, OwnerBadge } from '@/components/LeadWorkflow';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Input, Label } from '@/components/ui/Input';
import { PriorityBadge, ScoreChip } from '@/components/ui/Badge';
import { useToast } from '@/components/ui/Toast';
import { mergeLeadSnapshot } from '@/lib/workspace';
import type { LeadFilters, LeadResult, SavedSegment, WorkspaceLead } from '@/types';

const LeadDetailDrawer = lazy(() => import('@/components/LeadDetailDrawer').then((module) => ({ default: module.LeadDetailDrawer })));
const PAGE_SIZE = 50;
const selectClass = 'w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-700';

export function LeadWorkspacePage() {
  const qc = useQueryClient();
  const { toast } = useToast();
  const [filters, setFilters] = useState<LeadFilters>({});
  const [page, setPage] = useState(0);
  const [selectedSegment, setSelectedSegment] = useState('');
  const [groupDirty, setGroupDirty] = useState(false);
  const [showGroupForm, setShowGroupForm] = useState(false);
  const [groupName, setGroupName] = useState('');
  const [saving, setSaving] = useState(false);
  const [groupError, setGroupError] = useState('');
  const [selected, setSelected] = useState<{ jobId: string; lead: LeadResult } | null>(null);
  const opening = useRef(0);
  const team = useQuery({ queryKey: ['workspace-team'], queryFn: getTeam, refetchInterval: 60_000 });
  const segments = useQuery({ queryKey: ['workspace-segments'], queryFn: getSegments, refetchInterval: 60_000 });
  const activeSegment = segments.data?.find((group) => group.id === selectedSegment);
  const deferredSearch = useDeferredValue(filters.search?.trim() || '');
  const requestFilters = { ...filters, search: deferredSearch || undefined };
  const segmentId = selectedSegment && !groupDirty ? selectedSegment : undefined;
  const leads = useQuery({
    queryKey: ['workspace-leads', segmentId, activeSegment?.version, segmentId ? null : requestFilters, page],
    queryFn: () => getWorkspaceLeads(requestFilters, page * PAGE_SIZE, PAGE_SIZE, segmentId),
    refetchInterval: 60_000,
  });
  const total = leads.data?.total || 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  useEffect(() => {
    if (activeSegment && !groupDirty) { setFilters(activeSegment.filters); setGroupName(activeSegment.name); }
  }, [activeSegment, groupDirty]);
  useEffect(() => {
    if (selectedSegment && segments.isSuccess && !segments.isFetching && !activeSegment) {
      setSelectedSegment(''); setGroupDirty(true);
      setGroupError('This saved group was removed elsewhere. Your current filters are retained; you can save them as a new group.');
    }
  }, [selectedSegment, activeSegment, segments.isSuccess, segments.isFetching]);
  useEffect(() => {
    if (leads.data && page > Math.max(0, Math.ceil(leads.data.total / PAGE_SIZE) - 1)) setPage(Math.max(0, Math.ceil(leads.data.total / PAGE_SIZE) - 1));
  }, [leads.data, page]);

  const change = (next: Partial<LeadFilters>) => { setFilters((current) => ({ ...current, ...next })); setGroupDirty(true); setPage(0); };
  const chooseGroup = (group: SavedSegment | null) => {
    setSelectedSegment(group?.id || ''); setFilters(group?.filters || {}); setGroupName(group?.name || '');
    setGroupDirty(false); setPage(0); setShowGroupForm(false); setGroupError('');
  };
  const saveGroup = async (replace: boolean) => {
    if (saving || !groupName.trim()) return;
    setSaving(true); setGroupError('');
    try {
      const saved = replace && activeSegment
        ? await updateSegment(activeSegment.id, activeSegment.version, groupName.trim(), filters)
        : await createSegment(groupName.trim(), filters);
      await qc.invalidateQueries({ queryKey: ['workspace-segments'] });
      chooseGroup(saved);
      await qc.invalidateQueries({ queryKey: ['workspace-leads'] });
      toast(replace ? 'Saved group updated' : 'Saved group created', 'success');
    } catch (cause) {
      const conflict = isAxiosError(cause) && cause.response?.status === 409;
      setGroupDirty(true);
      if (conflict) { await segments.refetch(); setGroupError('This group changed elsewhere or the name already exists. Your filters and name are retained. Review the saved groups before saving again.'); }
      else {
        const detail = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
        setGroupError(typeof detail === 'string' ? detail : 'Could not save this group. Your filters are retained.');
      }
    } finally { setSaving(false); }
  };
  const removeGroup = async () => {
    if (saving || !activeSegment || !confirm(`Delete saved group "${activeSegment.name}"? Lead records will remain available.`)) return;
    setSaving(true); setGroupError('');
    try {
      await deleteSegment(activeSegment.id, activeSegment.version);
      chooseGroup(null);
      await qc.invalidateQueries({ queryKey: ['workspace-segments'] });
      toast('Saved group deleted', 'success');
    } catch (cause) {
      setGroupDirty(true);
      setGroupError(isAxiosError(cause) && cause.response?.status === 409 ? 'This group changed elsewhere. The latest version is loaded; review it before deleting.' : 'Could not delete this group. Please try again.');
      await segments.refetch();
    } finally { setSaving(false); }
  };
  const openLead = async (item: WorkspaceLead) => {
    const request = ++opening.current;
    try {
      const lead = await getLeadDetail(item.job_id, item.lead_id);
      if (request === opening.current) setSelected({ jobId: item.job_id, lead });
    } catch { if (request === opening.current) toast('Could not load this lead', 'error'); }
  };
  const savedLead = (lead: LeadResult) => setSelected((current) => current?.lead.lead_id === lead.lead_id ? { ...current, lead: mergeLeadSnapshot(current.lead, lead) } : current);

  return <div className="min-h-screen bg-sky-gradient">
    <TopBar />
    <main className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><h1 className="text-2xl font-bold text-slate-800">Lead workspace</h1><p className="mt-1 text-sm text-slate-500">All uploads, with one current record per lead. Owner, review, notes, and follow-up stay shared.</p></div>
        <Link to="/dashboard"><Button variant="outline" size="sm" icon={<Users className="h-4 w-4" />}>Team &amp; reminders</Button></Link>
      </div>
      <Card className="p-5 space-y-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="w-full sm:w-80"><Label htmlFor="lead-segment">Saved lead group</Label><select id="lead-segment" value={selectedSegment} disabled={segments.isPending || saving} onChange={(event) => chooseGroup(segments.data?.find((group) => group.id === event.target.value) || null)} className={selectClass}><option value="">All leads / custom filters</option>{segments.data?.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</select></div>
          <div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" icon={<Bookmark className="h-3.5 w-3.5" />} disabled={saving} onClick={() => { setShowGroupForm((value) => !value); setGroupError(''); }}>Save filters as a group</Button>{activeSegment && <Button size="sm" variant="ghost" icon={<Trash2 className="h-3.5 w-3.5" />} disabled={saving} onClick={() => void removeGroup()}>Delete group</Button>}</div>
        </div>
        <p className="text-xs text-slate-500">Groups store filters and automatically include new matching leads.{activeSegment && groupDirty ? ' You have adjusted this group’s filters; save to update the group.' : ''}</p>
        {segments.isError && <p role="alert" className="text-sm text-rose-700">Could not load saved groups. <button className="underline" onClick={() => void segments.refetch()}>Retry</button></p>}
        {showGroupForm && <form onSubmit={(event) => { event.preventDefault(); void saveGroup(!!activeSegment); }} className="rounded-xl bg-brand-50 p-4 space-y-3">
          <div><Label htmlFor="segment-name">Group name</Label><Input id="segment-name" value={groupName} required maxLength={100} disabled={saving} placeholder="High-priority leads in India" onChange={(event) => { setGroupName(event.target.value); setGroupDirty(true); }} /></div>
          <div className="flex flex-wrap gap-2"><Button type="submit" size="sm" variant="secondary" loading={saving} disabled={!groupName.trim()}>{activeSegment ? 'Update selected group' : 'Create group'}</Button>{activeSegment && <Button type="button" size="sm" variant="outline" disabled={saving || !groupName.trim()} icon={<Plus className="h-3.5 w-3.5" />} onClick={() => void saveGroup(false)}>Save as new group</Button>}<Button type="button" size="sm" variant="ghost" disabled={saving} onClick={() => setShowGroupForm(false)}>Cancel</Button></div>
        </form>}
        {groupError && <p role="alert" className="text-sm text-rose-700">{groupError}</p>}
        <fieldset disabled={saving} className="border-t border-slate-100 pt-4 disabled:opacity-70">
          <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-700"><Filter className="h-4 w-4" />Lead filters</h2>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div><Label htmlFor="lead-search">Search</Label><div className="relative"><Search className="absolute left-3 top-3 h-4 w-4 text-slate-400" /><Input id="lead-search" className="pl-9" value={filters.search || ''} maxLength={100} placeholder="Company, contact, email…" onChange={(event) => change({ search: event.target.value })} /></div></div>
            <div><Label htmlFor="lead-priority">Priority</Label><select id="lead-priority" className={selectClass} value={filters.priority || ''} onChange={(event) => change({ priority: (event.target.value || null) as LeadFilters['priority'] })}><option value="">Any priority</option><option value="High">High</option><option value="Medium">Medium</option><option value="Low">Low</option></select></div>
            <div><Label htmlFor="lead-owner">Owner</Label><select id="lead-owner" className={selectClass} value={filters.unassigned ? 'unassigned' : filters.owner_id || ''} onChange={(event) => change({ unassigned: event.target.value === 'unassigned', owner_id: event.target.value && event.target.value !== 'unassigned' ? event.target.value : null })}><option value="">All owners</option><option value="unassigned">Unassigned</option>{filters.owner_id && !team.data?.some((member) => member.id === filters.owner_id) && <option value={filters.owner_id}>Saved group owner</option>}{team.data?.map((member) => <option key={member.id} value={member.id}>{member.name}{member.active ? '' : ' (inactive)'}</option>)}</select>{team.isError && <p className="mt-1 text-xs text-rose-700">Owner names unavailable. <button className="underline" onClick={() => void team.refetch()}>Retry</button></p>}</div>
            <div><Label htmlFor="lead-review">Review status</Label><select id="lead-review" className={selectClass} value={filters.reviewed == null ? '' : String(filters.reviewed)} onChange={(event) => change({ reviewed: event.target.value === '' ? null : event.target.value === 'true' })}><option value="">All leads</option><option value="true">Reviewed</option><option value="false">Not reviewed</option></select></div>
            <div><Label htmlFor="lead-follow">Follow-up</Label><select id="lead-follow" className={selectClass} value={filters.follow_up || 'all'} onChange={(event) => change({ follow_up: event.target.value as LeadFilters['follow_up'] })}><option value="all">Any follow-up status</option><option value="due">Due</option><option value="upcoming">Upcoming</option><option value="completed">Completed</option><option value="unscheduled">Not scheduled</option></select></div>
            <div><Label htmlFor="lead-evidence">Evidence freshness</Label><select id="lead-evidence" className={selectClass} value={filters.evidence || 'all'} onChange={(event) => change({ evidence: event.target.value as LeadFilters['evidence'] })}><option value="all">Any evidence age</option><option value="fresh">Fresh (≤ 90 days)</option><option value="stale">Stale (&gt; 90 days)</option><option value="unknown">Date unknown</option></select></div>
            <div><Label htmlFor="lead-country">Country</Label><Input id="lead-country" value={filters.country || ''} maxLength={100} placeholder="e.g. India" onChange={(event) => change({ country: event.target.value })} /></div>
            <div><Label htmlFor="lead-industry">Industry</Label><Input id="lead-industry" value={filters.industry || ''} maxLength={100} placeholder="e.g. Software" onChange={(event) => change({ industry: event.target.value })} /></div>
            <div><Label htmlFor="lead-min-score">Minimum score</Label><Input id="lead-min-score" type="number" min={0} max={100} value={filters.min_score ?? ''} onChange={(event) => change({ min_score: event.target.value === '' ? null : Math.min(100, Math.max(0, Number(event.target.value))) })} /></div>
          </div>
          <Button size="sm" variant="ghost" className="mt-3" onClick={() => chooseGroup(null)}>Clear filters</Button>
        </fieldset>
      </Card>
      <Card className="overflow-hidden">
        <div className="flex items-center justify-between border-b border-slate-100 px-5 py-4"><h2 className="text-sm font-semibold text-slate-700">{activeSegment?.name || 'All leads'}{activeSegment && groupDirty ? ' · adjusted filters' : ''}</h2><span className="text-xs text-slate-500">{leads.isFetching ? 'Loading…' : `${total.toLocaleString()} matching leads`}</span></div>
        {leads.isPending ? <p className="p-8 text-center text-sm text-slate-500">Loading leads…</p> : leads.isError ? <p role="alert" className="p-8 text-center text-sm text-rose-700">Could not load this lead group. <button className="underline" onClick={() => void leads.refetch()}>Retry</button></p> : !leads.data?.items.length ? <p className="p-8 text-center text-sm text-slate-500">No matching leads. Adjust the filters or upload new leads.</p> : <div className="overflow-x-auto">
          <table className="w-full text-sm"><thead className="bg-slate-50 text-left text-xs font-semibold text-slate-600"><tr>{['Company / contact', 'Score / priority', 'Owner', 'Follow-up', 'Evidence', 'Reviewed', 'Upload'].map((label) => <th key={label} className="px-4 py-3">{label}</th>)}</tr></thead>
            <tbody className="divide-y divide-slate-100">{leads.data.items.map((item) => <tr key={item.lead_id} className="hover:bg-brand-50/30">
              <td className="px-4 py-3"><button onClick={() => void openLead(item)} className="max-w-xs text-left hover:text-brand-700"><p className="font-medium text-slate-800">{item.company_name || 'Unknown company'}</p><p className="mt-1 text-xs text-slate-500">{item.contact_name || 'Unnamed contact'}</p>{item.email_address && <p className="mt-0.5 truncate text-xs text-slate-400">{item.email_address}</p>}</button></td>
              <td className="px-4 py-3"><div className="space-y-2"><ScoreChip score={item.final_score} /><div><PriorityBadge priority={item.priority} /></div></div></td>
              <td className="px-4 py-3"><button aria-label={`Manage owner for ${item.contact_name || item.company_name || 'lead'}`} onClick={() => void openLead(item)}><OwnerBadge lead={item} /></button></td>
              <td className="px-4 py-3"><button aria-label={`Manage follow-up for ${item.contact_name || item.company_name || 'lead'}`} onClick={() => void openLead(item)}><FollowUpBadge lead={item} /></button></td>
              <td className="px-4 py-3"><EvidenceBadge lead={item} /></td>
              <td className="px-4 py-3"><LeadTracking key={`${item.lead_id}-tracking`} lead={item} jobId={item.job_id} onSaved={savedLead} /></td>
              <td className="px-4 py-3"><Link to={`/jobs/${item.job_id}`} className="block max-w-[180px] truncate text-xs text-brand-700 hover:underline">{item.file_name || 'Open upload'}</Link><p className="mt-1 text-xs text-slate-400">Row {item.row_number ?? '—'}</p></td>
            </tr>)}</tbody>
          </table>
        </div>}
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 px-5 py-3 text-xs text-slate-500">
          <span>{total ? `${page * PAGE_SIZE + 1}–${Math.min((page + 1) * PAGE_SIZE, total)} of ${total.toLocaleString()}` : '0 leads'} · Page {page + 1} of {totalPages}</span>
          <div className="flex gap-1"><Button size="sm" variant="ghost" disabled={page === 0 || leads.isFetching} onClick={() => setPage((value) => Math.max(0, value - 1))} icon={<ChevronLeft className="h-4 w-4" />}>Previous</Button><Button size="sm" variant="ghost" disabled={!leads.data?.has_more || leads.isFetching} onClick={() => setPage((value) => value + 1)} icon={<ChevronRight className="h-4 w-4" />}>Next</Button></div>
        </div>
      </Card>
    </main>
    {selected && <Suspense fallback={null}><LeadDetailDrawer lead={selected.lead} jobId={selected.jobId} open onClose={() => { opening.current += 1; setSelected(null); }} onSaved={savedLead} /></Suspense>}
  </div>;
}
