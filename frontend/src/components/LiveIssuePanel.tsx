import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { AlertTriangle, Radio, RefreshCw } from 'lucide-react';
import { getJobEvents } from '@/api/admin';
import { useToast } from '@/components/ui/Toast';
import type { OperationalEvent } from '@/types/admin';

export function eventTime(value: string) {
  const date = new Date(/(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : value + 'Z');
  return Number.isFinite(date.getTime()) ? date.toLocaleString() : 'Time unavailable';
}

export function EventList({ items }: { items: OperationalEvent[] }) {
  return <div className="divide-y divide-slate-100">{items.map((item) => <div key={item.event_id} className="px-4 py-3">
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className={`rounded-full px-2 py-0.5 font-semibold ${item.kind === 'rate_limit' ? 'bg-amber-100 text-amber-900' : item.severity === 'ERROR' ? 'bg-rose-100 text-rose-800' : 'bg-yellow-50 text-yellow-900'}`}>{item.kind === 'rate_limit' ? 'RATE LIMIT' : item.severity}</span>
      <time className="text-slate-500">{eventTime(item.occurred_at)}</time>
      {item.stage && <span className="font-medium text-slate-700">{item.stage.replace(/_/g, ' ')}</span>}
      {item.retry_after != null && <span className="text-amber-800">Retry after {item.retry_after}s</span>}
    </div>
    <p className="mt-1.5 break-words text-sm leading-relaxed text-slate-800">{item.message}</p>
    <div className="mt-1 flex flex-wrap gap-3 text-xs text-slate-500">
      {item.job_id && <Link className="text-brand-700 hover:underline" to={`/jobs/${item.job_id}`}>{item.file_name || `Job ${item.job_id.slice(0, 8)}`}</Link>}
      {!item.job_id && <span>System event</span>}
      {(item.company_name || item.contact_name) && <span>{[item.company_name, item.contact_name].filter(Boolean).join(' · ')}</span>}
      {item.lead_id && <span title={item.lead_id}>Lead {item.lead_id.slice(0, 8)}</span>}
      {item.component && <span>{item.component}</span>}
    </div>
  </div>)}</div>;
}

export function LiveIssuePanel({ jobId }: { jobId: string }) {
  return <JobIssues key={jobId} jobId={jobId} />;
}

function JobIssues({ jobId }: { jobId: string }) {
  const [live, setLive] = useState(true);
  const seen = useRef<Set<string> | null>(null);
  const { toast } = useToast();
  const query = useQuery({ queryKey: ['job-events', jobId], queryFn: ({ signal }) => getJobEvents(jobId, signal),
    enabled: !!jobId, refetchInterval: live ? 3000 : false, staleTime: 0, retry: 1 });
  useEffect(() => {
    if (!query.data) return;
    const ids = new Set(query.data.items.map((item) => item.event_id));
    if (seen.current) {
      const fresh = query.data.items.filter((item) => !seen.current!.has(item.event_id));
      if (fresh.length) toast(`${fresh.length} new processing ${fresh.length === 1 ? 'issue' : 'issues'}${fresh.some((item) => item.kind === 'rate_limit') ? ' · rate limit reported' : ''}. See the live log.`, fresh.some((item) => item.severity === 'ERROR') ? 'error' : 'info');
    }
    seen.current = ids;
  }, [query.data, toast]);
  return <section className="my-5 w-full overflow-hidden rounded-2xl border border-amber-200 bg-white" aria-label="Processing warnings and errors">
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-amber-100 bg-amber-50/60 px-4 py-3">
      <div className="flex items-center gap-2 text-sm font-semibold text-slate-800"><AlertTriangle className="h-4 w-4 text-amber-600" />Processing warnings &amp; errors</div>
      <div className="flex items-center gap-3 text-xs"><label className="flex cursor-pointer items-center gap-1.5"><input type="checkbox" checked={live} onChange={(event) => setLive(event.target.checked)} /><Radio className={`h-3.5 w-3.5 ${live ? 'text-emerald-600' : 'text-slate-400'}`} />Live · 3s</label>
        <button type="button" aria-label="Refresh processing logs" onClick={() => void query.refetch()}><RefreshCw className={`h-3.5 w-3.5 ${query.isFetching ? 'animate-spin' : ''}`} /></button>
        <Link className="font-medium text-brand-700 hover:underline" to={`/admin/logs?job=${jobId}`}>Full history</Link></div>
    </div>
    <div aria-live="polite" className="max-h-72 overflow-y-auto">
      {!!query.data?.runtime?.cooldown_seconds && <p role="status" className="border-b border-amber-100 bg-amber-50 p-3 text-sm text-amber-900">Azure rate-limit cooldown · about {query.data.runtime.cooldown_seconds}s remaining. New lead starts wait and saved stages resume automatically.</p>}
      {query.data?.runtime?.concurrency != null && <p className="border-b border-slate-100 px-4 py-2 text-xs text-slate-500">Global active leads: {query.data.runtime.active ?? 'Unavailable'} / saved limit {query.data.runtime.concurrency}.</p>}
      {query.isError ? <p role="alert" className="p-4 text-sm text-rose-700">Could not refresh diagnostics. <button className="underline" onClick={() => void query.refetch()}>Retry</button></p>
      : query.isPending ? <p className="p-4 text-sm text-slate-500">Loading diagnostics…</p>
      : query.data?.items.length ? <EventList items={query.data.items} /> : <p className="p-4 text-sm text-slate-500">No warnings or errors recorded for this job.</p>}
      {query.data?.warning && <p role="status" className="bg-amber-50 p-3 text-xs text-amber-900">{query.data.warning}</p>}
      {query.data?.has_more && <p className="p-3 text-xs text-slate-500">Showing the latest 50 events. Open Full history to see older diagnostics.</p>}
    </div>
  </section>;
}
