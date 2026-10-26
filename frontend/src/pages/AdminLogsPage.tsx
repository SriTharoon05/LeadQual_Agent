import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { getOperationalEvents } from '@/api/admin';
import { AdminError, AdminLayout } from '@/components/AdminLayout';
import { EventList } from '@/components/AdminEvents';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useDebouncedValue } from '@/hooks/useDebouncedValue';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function AdminLogsPage() {
  const [params] = useSearchParams();
  const [job, setJob] = useState(params.get('job') || '');
  const queryJob = params.get('job') || '';
  useEffect(() => { setJob(queryJob); }, [queryJob]);
  const [severity, setSeverity] = useState('');
  const [rates, setRates] = useState(false);
  const deferred = useDebouncedValue(job.trim());
  const valid = !deferred || UUID_PATTERN.test(deferred);
  return <AdminLayout title="Warnings & errors" description="Live operational diagnostics across jobs. Normal processing output is excluded.">
    <Card className="flex flex-wrap items-end gap-4 p-5"><label className="space-y-1 text-sm text-slate-600"><span>Job ID · optional</span><input className="block w-80 max-w-full rounded-lg border border-slate-200 px-3 py-2 text-sm" maxLength={36} placeholder="All jobs, or paste a complete job UUID" value={job} onChange={(event) => setJob(event.target.value)} /></label>
      <label className="space-y-1 text-sm text-slate-600"><span>Severity</span><select className="block rounded-lg border border-slate-200 px-3 py-2" value={severity} onChange={(event) => setSeverity(event.target.value)}><option value="">Warnings &amp; errors</option><option value="WARNING">Warnings</option><option value="ERROR">Errors</option></select></label>
      <label className="flex items-center gap-2 pb-2 text-sm text-slate-700"><input type="checkbox" checked={rates} onChange={(event) => setRates(event.target.checked)} />Rate limits only</label>
      <Button variant="outline" onClick={() => { setJob(''); setSeverity(''); setRates(false); }}>Clear filters</Button>
    </Card>
    {valid ? <Logs key={`${deferred}:${severity}:${rates}`} job={deferred} severity={severity} rates={rates} /> : <p className="text-sm text-amber-800">Enter a complete job UUID to apply this filter.</p>}
  </AdminLayout>;
}
function Logs({ job, severity, rates }: { job: string; severity: string; rates: boolean }) {
  const [cursors, setCursors] = useState<(number | undefined)[]>([undefined]);
  const [page, setPage] = useState(0);
  const [live, setLive] = useState(true);
  const query = useQuery({ queryKey: ['admin-logs', job, severity, rates, cursors[page]],
    queryFn: ({ signal }) => getOperationalEvents({ job_id: job || undefined, severity: severity || undefined, rate_only: rates, before_id: cursors[page], limit: 100 }, signal),
    refetchInterval: live && page === 0 ? 3000 : false, staleTime: 0 });
  return <><div className="flex items-center justify-between gap-3 text-sm text-slate-600"><label className="flex items-center gap-2"><input type="checkbox" checked={live} onChange={(event) => setLive(event.target.checked)} />Live updates · every 3 seconds</label><Button variant="outline" onClick={() => { if (page === 0) void query.refetch(); else { setPage(0); setCursors([undefined]); } }}>Latest events</Button></div>
    {page > 0 && <p className="text-xs text-slate-500">Viewing older events. Live updates are paused until you return to the latest page.</p>}
    {query.isError && <AdminError retry={() => void query.refetch()} />}
    {query.data?.warning && <p role="status" className="rounded-xl bg-amber-50 p-4 text-sm text-amber-900">{query.data.warning}</p>}
    <Card className="overflow-hidden">{query.isPending ? <p className="p-6 text-sm text-slate-500">Loading diagnostics…</p> : query.data?.items.length ? <EventList items={query.data.items} /> : <p className="p-6 text-sm text-slate-500">No matching warnings or errors.</p>}</Card>
    <div className="flex justify-end gap-3"><Button variant="outline" disabled={query.isFetching || page === 0} onClick={() => setPage(page - 1)}>Newer</Button><Button variant="outline" disabled={query.isFetching || !query.data?.has_more || !query.data?.before_id} onClick={() => { if (!query.data?.before_id) return; setCursors([...cursors.slice(0, page + 1), query.data.before_id]); setPage(page + 1); }}>Older</Button></div>
  </>;
}
