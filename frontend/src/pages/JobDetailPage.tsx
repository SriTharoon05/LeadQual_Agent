import { lazy, Suspense, useState, useEffect, useRef } from 'react';
import { useDebouncedValue } from '@/hooks/useDebouncedValue';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import {
  ArrowLeft, FileSpreadsheet, FileText, Trash2, RefreshCw, Search,
  ChevronUp, ChevronDown, ChevronLeft, ChevronRight,
  ChevronsLeft, ChevronsRight, Building2, Mail, Loader2, Target, Zap,
} from 'lucide-react';
import { TopBar } from '@/components/TopBar';
import { LeadTracking } from '@/components/LeadTracking';
import { EvidenceBadge, FollowUpBadge, OwnerBadge } from '@/components/LeadWorkflow';
import { invalidateLeadWorkspace, mergeLeadSnapshot } from '@/lib/workspace';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge, StatusBadge, PriorityBadge, ScoreChip } from '@/components/ui/Badge';
import { Input, Label, Textarea } from '@/components/ui/Input';
import { TableSkeleton } from '@/components/ui/Skeleton';
import { EmptyState } from '@/pages/DashboardPage';
import { useToast } from '@/components/ui/Toast';
import { getJobStatus, getResultsPage, deleteJob, startIcp, getLeadDetail } from '@/api/endpoints';
import { useExports } from '@/components/ExportManager';
import { dash, fmtDate, safeArr } from '@/lib/format';
import type { LeadResult, Priority } from '@/types';

const LeadDetailDrawer = lazy(() => import('@/components/LeadDetailDrawer').then((module) => ({ default: module.LeadDetailDrawer })));
const ChatWidget = lazy(() => import('@/components/ChatWidget').then((module) => ({ default: module.ChatWidget })));

type SortKey = 'company_name' | 'contact_name' | 'email_address' | 'final_score' | 'priority';
type SortDir = 'asc' | 'desc';

// Server-side page size. Each "page" is one network request. 50 keeps requests
// small + fast while minimising round-trips. Next 2 pages are prefetched.
const PAGE_SIZE = 50;
const PREFETCH_AHEAD = 2;

export function JobDetailPage() {
  const { jobId = '' } = useParams();
  return <JobDetailContent key={jobId} jobId={jobId} />;
}

function JobDetailContent({ jobId }: { jobId: string }) {
  const navigate = useNavigate();
  const { toast } = useToast();
  const qc = useQueryClient();
  const exports = useExports();

  const [search, setSearch] = useState('');
  const [priorityFilter, setPriorityFilter] = useState<Priority | 'all'>('all');
  const [sortKey, setSortKey] = useState<SortKey>('final_score');
  const [sortDir, setSortDir] = useState<SortDir>('desc');
  const [page, setPage] = useState(0);                 // ← server page index (0-based)
  const [selectedLead, setSelectedLead] = useState<LeadResult | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [resumeIcp, setResumeIcp] = useState('');
  const [starting, setStarting] = useState(false);
  const startingRef = useRef(false);
  const opening = useRef(0);
  const detailRequest = useRef<AbortController | null>(null);
  useEffect(() => () => { opening.current++; detailRequest.current?.abort(); }, []);
  const deferredSearch = useDebouncedValue(search.trim());

  // ── Job status (live poll while running) ──────────────────────────────────
  const { data: status, isPending: statusLoading, isError: statusError, refetch: reloadStatus } = useQuery({
    queryKey: ['job-status', jobId],
    queryFn: ({ signal }) => getJobStatus(jobId, signal),
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === 'completed' || s === 'failed' || s === 'import_failed' ? false : 3000;
    },
    placeholderData: (prev) => prev,
    refetchOnWindowFocus: false,
  });

  const isCompleted = status?.status === 'completed';
  const isAwaitingIcp = status?.status === 'queued';
  const isRunning = status?.status === 'running' || status?.status === 'importing';

  // ── Current page of results (server-side, windowed) ───────────────────────
  const {
    data: pageResult,
    isLoading,
    isFetching,
    isError: resultsError,
    isPlaceholderData,
    refetch: reloadResults,
  } = useQuery({
    queryKey: ['job-results', jobId, page, PAGE_SIZE, deferredSearch, priorityFilter, sortKey, sortDir],
    queryFn: ({ signal }) => getResultsPage(jobId, page * PAGE_SIZE, PAGE_SIZE, {
      search: deferredSearch, priority: priorityFilter, sortBy: sortKey, sortDir,
    }, signal),
    enabled: isCompleted || status?.status === 'failed',
    placeholderData: (prev, previousQuery) => previousQuery?.queryKey[4] === deferredSearch
      && previousQuery.queryKey[5] === priorityFilter && previousQuery.queryKey[6] === sortKey
      && previousQuery.queryKey[7] === sortDir ? prev : undefined,
    refetchOnWindowFocus: false,
  });

  const results: LeadResult[] = pageResult?.items ?? [];
  const total = pageResult?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.min(page, totalPages - 1);
  useEffect(() => {
    if (pageResult && !isPlaceholderData && page >= totalPages) setPage(Math.max(0, totalPages - 1));
  }, [pageResult, page, totalPages, isPlaceholderData]);

  // ── Prefetch the next N pages so "Next" feels instant ─────────────────────
  useEffect(() => {
    if (!isCompleted || search.trim() !== deferredSearch) return;
    for (let i = 1; i <= PREFETCH_AHEAD; i++) {
      const next = currentPage + i;
      if (next >= totalPages) break;
      qc.prefetchQuery({
        queryKey: ['job-results', jobId, next, PAGE_SIZE, deferredSearch, priorityFilter, sortKey, sortDir],
        queryFn: ({ signal }) => getResultsPage(jobId, next * PAGE_SIZE, PAGE_SIZE, {
          search: deferredSearch, priority: priorityFilter, sortBy: sortKey, sortDir,
        }, signal),
      });
    }
  }, [currentPage, totalPages, isCompleted, jobId, qc, deferredSearch, search, priorityFilter, sortKey, sortDir]);

  const fileName = status?.file_name ?? null;

  const filtered = safeArr(results);

  const toggleSort = (key: SortKey) => {
    setPage(0);
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('desc');
    }
  };

  const openLead = async (lead: LeadResult) => {
    const request = ++opening.current;
    detailRequest.current?.abort();
    const controller = new AbortController();
    detailRequest.current = controller;
    try {
      const fresh = await getLeadDetail(jobId, lead.lead_id, controller.signal);
      if (request === opening.current) { setSelectedLead(fresh); setDrawerOpen(true); }
    } catch { if (request === opening.current) toast('Could not load lead details', 'error'); }
  };
  const trackingSaved = (lead: LeadResult) => {
    setSelectedLead((current) => current?.lead_id === lead.lead_id ? mergeLeadSnapshot(current, lead) : current);
  };

  const goToPage = (p: number) => setPage(Math.max(0, Math.min(totalPages - 1, p)));

  const handleDelete = async () => {
    if (!confirm('Delete this job and all its results?')) return;
    try {
      await deleteJob(jobId);
      toast('Job deleted', 'success');
      qc.invalidateQueries({ queryKey: ['jobs'] });
      void invalidateLeadWorkspace(qc);
      navigate('/dashboard');
    } catch {
      toast('Failed to delete job', 'error');
    }
  };

  const handleRefresh = () => {
    qc.invalidateQueries({ queryKey: ['job-results', jobId] });
  };

  const handleStartIcp = async () => {
    if (!resumeIcp.trim() || startingRef.current) return;
    startingRef.current = true;
    setStarting(true);
    try {
      await startIcp(jobId, resumeIcp.trim());
      toast('Processing started; you can safely return to the dashboard', 'success');
      await qc.invalidateQueries({ queryKey: ['job-status', jobId] });
      await qc.invalidateQueries({ queryKey: ['jobs'] });
    } catch {
      try {
        const current = await getJobStatus(jobId);
        if (['running', 'completed', 'failed'].includes(current.status)) {
          qc.setQueryData(['job-status', jobId], current);
          toast('Processing was accepted. The current status is now loaded.', 'info');
          return;
        }
      } catch { /* Retain the ICP draft for a deliberate retry. */ }
      toast('Could not confirm processing. Reload the upload status before trying again.', 'error');
    } finally {
      startingRef.current = false;
      setStarting(false);
    }
  };

  const sortIcon = (col: SortKey) =>
    sortKey === col ? (
      sortDir === 'asc' ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />
    ) : null;

  return (
    <div className="min-h-screen bg-sky-gradient">
      <TopBar />
      <main className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8 py-8 space-y-6">
        {/* Back link */}
        <Link to="/dashboard" className="inline-flex items-center gap-1.5 text-sm text-slate-500 hover:text-brand-600 transition-colors">
          <ArrowLeft className="h-4 w-4" /> Back to Dashboard
        </Link>

        {statusError && <div role="alert" className="rounded-xl bg-rose-50 p-4 text-sm text-rose-700">Could not load this upload. <button className="underline" onClick={() => void reloadStatus()}>Retry</button></div>}

        {/* Job header */}
        <Card className="p-6">
          <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
            <div>
              <div className="flex items-center gap-3 mb-2">
                <h1 className="text-xl font-bold text-slate-800">{dash(fileName)}</h1>
                <StatusBadge status={status?.status} />
              </div>
              <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-slate-500">
                <span>Created: {fmtDate(status?.created_at)}</span>
                <span>Completed: {fmtDate(status?.completed_at)}</span>
                <span>Rows: {status?.total_rows ?? '—'}</span>
                <span>Processed: {status?.processed_rows ?? '—'}</span>
                {(status?.failed_rows ?? 0) > 0 && <span className="text-rose-600">Failed: {status?.failed_rows}</span>}
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant="outline" icon={<RefreshCw className="h-3.5 w-3.5" />}
                onClick={handleRefresh}>
                Refresh
              </Button>
              <Button size="sm" variant="ghost" icon={exports.isBusy(jobId, 'excel') ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileSpreadsheet className="h-4 w-4 text-emerald-600" />}
                onClick={() => exports.start(jobId, 'excel')} disabled={!isCompleted || exports.isBusy(jobId, 'excel')}>
                Excel
              </Button>
              <Button size="sm" variant="ghost" icon={exports.isBusy(jobId, 'csv') ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileText className="h-4 w-4 text-sky-600" />}
                onClick={() => exports.start(jobId, 'csv')} disabled={!isCompleted || exports.isBusy(jobId, 'csv')}>
                CSV
              </Button>
              <Button size="sm" variant="danger" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={handleDelete}>
                Delete
              </Button>
            </div>
          </div>

          {/* Progress bar if running */}
          {isRunning && (
            <div className="mt-4">
              <div className="flex justify-between text-xs text-slate-500 mb-1.5">
                <span>Processing…</span>
                <span className="font-semibold tabular-nums">{(status?.progress_pct ?? 0).toFixed(0)}%</span>
              </div>
              <div className="h-2.5 rounded-full bg-slate-100 overflow-hidden">
                <motion.div
                  className="h-full rounded-full bg-gradient-to-r from-brand-400 to-brand-600"
                  animate={{ width: `${status?.progress_pct ?? 0}%` }}
                  transition={{ duration: 0.5 }}
                />
              </div>
            </div>
          )}
        </Card>

        {isAwaitingIcp && (
          <Card className="p-6">
            <div className="flex items-center gap-2 mb-1">
              <Target className="h-5 w-5 text-brand-500" />
              <h2 className="text-base font-semibold text-slate-800">Import ready — define the ICP</h2>
            </div>
            <p className="text-sm text-slate-500 mb-4">This job was imported but qualification has not started. Describe the target customer to resume it.</p>
            <Label htmlFor="resume-icp">Ideal Customer Profile</Label>
            <Textarea
              id="resume-icp"
              rows={4}
              value={resumeIcp}
              onChange={(event) => setResumeIcp(event.target.value)}
              placeholder="B2B companies in the target industry, geography, employee and revenue range…"
            />
            <div className="mt-4 flex justify-end">
              <Button onClick={handleStartIcp} disabled={!resumeIcp.trim() || starting} icon={starting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Zap className="h-4 w-4" />}>
                {starting ? 'Starting…' : 'Start Background Processing'}
              </Button>
            </div>
          </Card>
        )}

        {status && (status.reused_rows > 0 || status.research_reused_rows > 0) && (
          <div className="rounded-xl border border-sky-200 bg-sky-50 px-5 py-3 text-sm text-sky-900">
            <strong>{status.reused_rows || 0}</strong> saved results reused · <strong>{status.research_reused_rows || 0}</strong> leads qualified using saved research · <strong>{Math.max(0, status.processed_rows - status.failed_rows - (status.reused_rows || 0) - (status.research_reused_rows || 0))}</strong> newly researched
          </div>
        )}
        {/* Results table */}
        <Card className="overflow-hidden">
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 px-6 py-4 border-b border-slate-100">
            <h3 className="text-sm font-semibold text-slate-700 flex items-center gap-2">
              Lead Results
              <Badge color="brand" className="ml-1">{total}</Badge>
              {isFetching && !isLoading && (
                <Loader2 className="h-3.5 w-3.5 animate-spin text-brand-400" aria-label="Loading page" />
              )}
            </h3>
            <div className="flex flex-col sm:flex-row gap-2">
              <div className="relative">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400" />
                <Input
                  placeholder="Search all leads…"
                  maxLength={100}
                  value={search}
                  onChange={(e) => { setSearch(e.target.value); setPage(0); }}
                  className="pl-9 sm:w-56"
                />
              </div>
              <select
                value={priorityFilter}
                onChange={(e) => { setPriorityFilter(e.target.value as Priority | 'all'); setPage(0); }}
                className="rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-700 focus:border-brand-400 focus:outline-none focus:ring-2 focus:ring-brand-100"
              >
                <option value="all">All Priorities</option>
                <option value="High">High</option>
                <option value="Medium">Medium</option>
                <option value="Low">Low</option>
              </select>
            </div>
          </div>

          {resultsError ? (
            <div role="alert" className="p-6 text-sm text-rose-700">Could not load these leads. <button className="underline" onClick={() => void reloadResults()}>Retry</button></div>
          ) : statusLoading || isLoading ? (
            <div className="p-6"><TableSkeleton /></div>
          ) : isRunning ? (
            <EmptyState
              icon={<RefreshCw className="h-10 w-10 animate-spin" />}
              title="Job still processing"
              subtitle="Results will appear here once the job completes"
            />
          ) : total === 0 ? (
            <EmptyState
              icon={<Building2 className="h-10 w-10" />}
              title="No results yet"
              subtitle="No saved lead records match the current filters"
            />
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={<Building2 className="h-10 w-10" />}
              title="No matching leads on this page"
              subtitle="Try clearing the search or priority filter"
            />
          ) : (
            <>
              {/* Desktop table */}
              <div className="hidden md:block overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-slate-100 bg-slate-50/50">
                      <Th>Row</Th>
                      <Th onClick={() => toggleSort('company_name')}><span className="flex items-center gap-1">Company {sortIcon('company_name')}</span></Th>
                      <Th onClick={() => toggleSort('contact_name')}><span className="flex items-center gap-1">Contact {sortIcon('contact_name')}</span></Th>
                      <Th>Email</Th>
                      <Th onClick={() => toggleSort('final_score')}><span className="flex items-center gap-1">Score {sortIcon('final_score')}</span></Th>
                      <Th onClick={() => toggleSort('priority')}><span className="flex items-center gap-1">Priority {sortIcon('priority')}</span></Th>
                      <Th>Reviewed</Th>
                      <Th>Owner / follow-up</Th>
                      <Th>Evidence</Th>
                      <Th>Notes / history</Th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-50">
                    {filtered.map((lead) => (
                      <tr
                        key={lead.lead_id}
                        onClick={() => openLead(lead)}
                        className="hover:bg-brand-50/40 transition-colors cursor-pointer"
                      >
                        <td className="px-4 py-3 text-xs text-slate-500" title="Stored data row; use this number in chat">{lead.row_number ?? '—'}</td>
                        <td className="px-6 py-3 font-medium text-slate-800 max-w-[200px] truncate">{dash(lead.company_name)}</td>
                        <td className="px-4 py-3 text-slate-600">{dash(lead.contact_name)}</td>
                        <td className="px-4 py-3 text-slate-600 max-w-[180px] truncate">{lead.email_address ?? '—'}</td>
                        <td className="px-4 py-3"><ScoreChip score={lead.final_score} /></td>
                        <td className="px-4 py-3"><PriorityBadge priority={lead.priority} /></td>
                        <td className="px-4 py-3"><LeadTracking lead={lead} jobId={jobId} onSaved={trackingSaved} /></td>
                        <td className="px-4 py-3"><div className="space-y-1"><OwnerBadge lead={lead} /><div><FollowUpBadge lead={lead} /></div></div></td>
                        <td className="px-4 py-3"><EvidenceBadge lead={lead} /></td>
                        <td className="px-4 py-3 max-w-[180px]">
                          <button type="button" onClick={(event) => { event.stopPropagation(); void openLead(lead); }}
                            className="block max-w-full truncate text-left text-xs text-brand-700 hover:underline">
                            {lead.tracking_notes || 'Add notes'}
                          </button>
                          {lead.reuse_kind && <span className="mt-1 block text-xs text-sky-700">{lead.reuse_kind === 'full' ? 'Result reused' : 'Research reused'}</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* Mobile cards */}
              <div className="md:hidden divide-y divide-slate-50">
                {filtered.map((lead) => (
                  <div key={lead.lead_id} onClick={() => openLead(lead)} className="px-4 py-3 hover:bg-brand-50/40 cursor-pointer">
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-slate-800 truncate">{dash(lead.company_name)}</span>
                      <ScoreChip score={lead.final_score} />
                    </div>
                    <div className="flex items-center gap-2 mt-1 text-xs text-slate-500">
                      <span>{dash(lead.contact_name)}</span>
                      <PriorityBadge priority={lead.priority} />
                    </div>
                    <div className="my-2 flex items-center gap-2 text-xs text-slate-500">
                      <LeadTracking lead={lead} jobId={jobId} onSaved={trackingSaved} /> Reviewed
                      <button type="button" onClick={(event) => { event.stopPropagation(); void openLead(lead); }} className="text-brand-700 underline">{lead.tracking_notes ? 'View notes' : 'Add notes'}</button>
                      {lead.reuse_kind && <span className="text-sky-700">{lead.reuse_kind === 'full' ? 'Result reused' : 'Research reused'}</span>}
                    </div>
                    <div className="mb-2 flex flex-wrap gap-2"><OwnerBadge lead={lead} /><FollowUpBadge lead={lead} /><EvidenceBadge lead={lead} /></div>
                    {lead.email_address && (
                      <div className="flex items-center gap-1 mt-1 text-xs text-slate-400">
                        <Mail className="h-3 w-3" /> {lead.email_address}
                      </div>
                    )}
                  </div>
                ))}
              </div>

              {/* Server-side pagination controls */}
              <div className="flex flex-col sm:flex-row items-center justify-between gap-3 px-6 py-3 border-t border-slate-100">
                <span className="text-xs text-slate-500">
                  Showing{' '}
                  <span className="font-semibold tabular-nums">
                    {currentPage * PAGE_SIZE + 1}–{Math.min((currentPage + 1) * PAGE_SIZE, total)}
                  </span>{' '}
                  of <span className="font-semibold tabular-nums">{total}</span> leads
                </span>

                <div className="flex items-center gap-1">
                  <PagerBtn onClick={() => goToPage(0)} disabled={currentPage === 0} title="First page">
                    <ChevronsLeft className="h-4 w-4" />
                  </PagerBtn>
                  <PagerBtn onClick={() => goToPage(currentPage - 1)} disabled={currentPage === 0} title="Previous">
                    <ChevronLeft className="h-4 w-4" />
                  </PagerBtn>

                  <span className="px-3 text-xs font-semibold text-slate-600 tabular-nums">
                    Page {currentPage + 1} of {totalPages}
                  </span>

                  <PagerBtn onClick={() => goToPage(currentPage + 1)} disabled={currentPage >= totalPages - 1} title="Next">
                    <ChevronRight className="h-4 w-4" />
                  </PagerBtn>
                  <PagerBtn onClick={() => goToPage(totalPages - 1)} disabled={currentPage >= totalPages - 1} title="Last page">
                    <ChevronsRight className="h-4 w-4" />
                  </PagerBtn>
                </div>
              </div>
            </>
          )}
        </Card>
      </main>

      {/* Lead drawer */}
      {drawerOpen && (
        <Suspense fallback={null}>
          <LeadDetailDrawer jobId={jobId} onSaved={trackingSaved} lead={selectedLead} open={drawerOpen} onClose={() => { opening.current += 1; detailRequest.current?.abort(); setDrawerOpen(false); }} />
        </Suspense>
      )}

      {/* Chat widget (only when job completed) */}
      {isCompleted && <Suspense fallback={null}><ChatWidget jobId={jobId} /></Suspense>}
    </div>
  );
}

function Th({ children, onClick }: { children: React.ReactNode; onClick?: () => void }) {
  return (
    <th
      onClick={onClick}
      className={`px-6 py-3 text-left font-semibold text-slate-600 ${onClick ? 'cursor-pointer hover:text-brand-600 select-none' : ''} first:px-6 [&:not(:first-child)]:px-4`}
    >
      {children}
    </th>
  );
}

function PagerBtn({
  children, onClick, disabled, title,
}: { children: React.ReactNode; onClick: () => void; disabled?: boolean; title?: string }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      className="rounded-lg p-1.5 text-slate-500 hover:bg-brand-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
    >
      {children}
    </button>
  );
}
