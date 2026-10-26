import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { ChevronLeft, ChevronRight, RefreshCw } from 'lucide-react';
import { getCostJobs, getJobCost, getLeadCost } from '@/api/admin';
import { AdminError, AdminLayout, costMoney, metric } from '@/components/AdminLayout';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { eventTime } from '@/components/AdminEvents';
import { useDebouncedValue } from '@/hooks/useDebouncedValue';
import type { CostMetrics, Pricing, StageCost } from '@/types/admin';

const cell = 'whitespace-nowrap px-4 py-3 text-left text-sm';
function recordedCost(value: CostMetrics, currency = 'USD') {
  if (!value.calls) return 'No usage data';
  if (value.unpriced_calls === value.calls && !value.total_usd) return 'Not priced';
  return costMoney(currency === 'INR' ? value.total_inr : value.total_usd, currency);
}
function EstimateNotice() {
  return <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm leading-relaxed text-amber-900">Usage-derived estimates, not an Azure invoice. New calls retain captured rates. Legacy calls use current reference rates and may lack cached-token, embedding or billable Bing-request usage. Missing charges are flagged instead of invented. Taxes and other Azure services are excluded. Usage may appear a few seconds after the request.</div>;
}
function Prices({ pricing }: { pricing: Pricing }) {
  return <Card className="space-y-3 p-5"><div className="flex flex-wrap justify-between gap-2"><h2 className="font-semibold text-slate-900">Current configured prices</h2><Link className="text-sm text-brand-700 underline" to="/admin/settings">Configure rates</Link></div>
    {!pricing.confirmed && <p className="text-xs text-amber-800">Reference prices have not yet been confirmed against your Azure contract.</p>}
    <div className="grid gap-3 text-sm sm:grid-cols-3 lg:grid-cols-5">{[
      [`${pricing.model} input`, pricing.input_per_million, '/ 1M tokens'],
      ['Cached input', pricing.cached_per_million, '/ 1M tokens'], ['Output', pricing.output_per_million, '/ 1M tokens'],
      [pricing.embedding_model, pricing.embedding_per_million, '/ 1M tokens'], ['Bing grounding', pricing.search_per_thousand, '/ 1K requests'],
    ].map(([name, value, unit]) => <div key={String(name)} className="rounded-lg bg-slate-50 p-3"><p className="text-xs text-slate-500">{name}</p><p className="mt-1 font-semibold text-slate-800">{costMoney(Number(value))}</p><p className="text-xs text-slate-400">{unit}</p></div>)}</div>
  </Card>;
}
function Summary({ value, fx, asOf }: { value: CostMetrics; fx?: number | null; asOf?: string | null }) {
  return <>
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">{[
      ['Known recorded cost · USD', recordedCost(value)], ['Converted estimate · INR', recordedCost(value, 'INR')],
      ['Input / cached / output tokens', `${metric(value.input_tokens)} / ${metric(value.cached_tokens)} / ${metric(value.output_tokens)}`],
      ['Billable Bing requests reported', metric(value.search_requests)],
    ].map(([label, amount]) => <Card key={label} className="p-5"><p className="text-xs text-slate-500">{label}</p><p className="mt-2 break-words text-lg font-bold text-slate-900">{amount}</p></Card>)}</div>
    <div className="text-xs text-slate-500">{metric(value.calls)} recorded attempts · {metric(value.failed_or_unreported_calls)} failed/unreported · {metric(value.incomplete_calls)} incomplete estimates · {metric(value.legacy_calls)} legacy calls · {metric(value.unpriced_calls)} attempts with unpriced token usage.
      {fx != null && <span> Conversion: ₹{fx} / USD{asOf ? `, reference ${eventTime(asOf)}` : ''}.</span>}
      {value.missing_search_usage > 0 && <p className="mt-1 font-medium text-amber-800">{metric(value.missing_search_usage)} calls are missing the provider's billable Bing request counter; their search fees are excluded. Visible tool items are diagnostic counts, not billable requests.</p>}
    </div>
  </>;
}
function Stages({ rows }: { rows: StageCost[] }) {
  return <Card className="overflow-hidden"><div className="border-b border-slate-100 p-5"><h2 className="font-semibold text-slate-900">Cost by processing step</h2><p className="mt-1 text-xs text-slate-500">Embedding, research, ICP, outreach, import mapping and chat appear when usage is recorded.</p></div>
    <div className="overflow-x-auto"><table className="w-full"><thead className="bg-slate-50 text-xs text-slate-500"><tr>{['Step / module', 'Attempts', 'Input', 'Cached', 'Output', 'Bing requests', 'Token USD', 'Search USD', 'Total USD', 'INR'].map((label) => <th scope="col" className={cell} key={label}>{label}</th>)}</tr></thead>
      <tbody className="divide-y divide-slate-100">{rows.map((row) => <tr key={`${row.stage}:${row.module}`}><td className={cell}><strong className="text-slate-800">{row.stage || 'Unspecified'}</strong><span className="block text-xs text-slate-500">{row.module}</span></td><td className={cell}>{metric(row.calls)}</td><td className={cell}>{metric(row.input_tokens)}</td><td className={cell}>{metric(row.cached_tokens)}</td><td className={cell}>{metric(row.output_tokens)}</td><td className={cell}>{metric(row.search_requests)}</td><td className={cell}>{row.unpriced_calls === row.calls ? 'Not priced' : costMoney(row.input_usd + row.cached_usd + row.output_usd)}</td><td className={cell}>{row.missing_search_usage === row.calls ? 'Not reported' : costMoney(row.search_usd)}</td><td className={`${cell} font-semibold`}>{recordedCost(row)}</td><td className={cell}>{recordedCost(row, 'INR')}</td></tr>)}</tbody></table>
      {!rows.length && <p className="p-5 text-sm text-slate-500">No usage recorded for these steps.</p>}</div>
  </Card>;
}
function Pager({ page, total, size, busy, onPage }: { page: number; total: number; size: number; busy: boolean; onPage: (page: number) => void }) {
  return <div className="flex items-center justify-end gap-3 text-sm text-slate-600"><Button variant="outline" disabled={busy || page === 0} onClick={() => onPage(page - 1)} icon={<ChevronLeft className="h-4 w-4" />}>Previous</Button><span>{total ? page * size + 1 : 0}–{Math.min(total, (page + 1) * size)} of {metric(total)}</span><Button variant="outline" disabled={busy || (page + 1) * size >= total} onClick={() => onPage(page + 1)} icon={<ChevronRight className="h-4 w-4" />}>Next</Button></div>;
}

export function AdminCostPage() {
  const { jobId, leadId } = useParams();
  if (jobId && leadId) return <LeadCosts key={`${jobId}:${leadId}`} jobId={jobId} leadId={leadId} />;
  if (jobId) return <JobCosts key={jobId} jobId={jobId} />;
  return <CostJobs />;
}
function CostJobs() {
  const [search, setSearch] = useState('');
  const deferred = useDebouncedValue(search.trim());
  return <AdminLayout title="AI usage & cost" description="Job → lead → step → individual API attempt, in USD and INR."><EstimateNotice />
    <input aria-label="Search jobs by filename" className="w-full max-w-md rounded-xl border border-slate-200 px-4 py-2 text-sm" placeholder="Search uploaded filename…" maxLength={100} value={search} onChange={(event) => setSearch(event.target.value)} />
    <CostJobResults key={deferred} deferred={deferred} />
  </AdminLayout>;
}
function CostJobResults({ deferred }: { deferred: string }) {
  const [page, setPage] = useState(0);
  const query = useQuery({ queryKey: ['admin-cost', 'jobs', deferred, page], queryFn: ({ signal }) => getCostJobs(page * 25, deferred, signal), refetchInterval: 15000 });
  useEffect(() => { if (query.data && page * 25 >= query.data.total && page > 0) setPage(Math.max(0, Math.ceil(query.data.total / 25) - 1)); }, [query.data, page]);
  return <>
    <div className="flex justify-end"><Button variant="outline" onClick={() => void query.refetch()} icon={<RefreshCw className="h-4 w-4" />}>Refresh costs</Button></div>
    {query.isError && <AdminError retry={() => void query.refetch()} />}
    {query.data && <><Prices pricing={query.data.pricing} /><p className="text-xs text-slate-500">{query.data.usd_inr ? `INR conversion: ₹${query.data.usd_inr} per USD · ${query.data.fx_as_of ? eventTime(query.data.fx_as_of) : 'Configured rate'}` : 'INR reference rate is unavailable. Set it in Runtime settings.'}</p></>}
    <Card className="overflow-hidden"><div className="overflow-x-auto"><table className="w-full"><thead className="bg-slate-50 text-slate-500"><tr>{['Upload / job', 'Status', 'Attempts', 'Input / output tokens', 'Bing requests', 'Known USD', 'INR', 'Coverage'].map((label) => <th scope="col" key={label} className={cell}>{label}</th>)}</tr></thead><tbody className="divide-y divide-slate-100">{query.data?.items.map((job) => <tr key={job.job_id} className="hover:bg-brand-50/30"><td className={cell}><Link className="font-semibold text-brand-700 hover:underline" to={`/admin/cost/${job.job_id}`}>{job.file_name || job.job_id}</Link><span className="block text-xs text-slate-400">{job.job_id.slice(0, 8)} · {metric(job.total_rows)} leads</span></td><td className={cell}>{job.status}</td><td className={cell}>{metric(job.calls)}</td><td className={cell}>{metric(job.input_tokens)} / {metric(job.output_tokens)}</td><td className={cell}>{metric(job.search_requests)}</td><td className={`${cell} font-semibold`}>{recordedCost(job)}</td><td className={cell}>{recordedCost(job, 'INR')}</td><td className={cell}><span className={`rounded-full px-2 py-1 text-xs ${job.incomplete_calls ? 'bg-amber-50 text-amber-900' : 'bg-slate-100 text-slate-600'}`}>{!job.calls ? 'No usage data' : job.incomplete_calls ? `${metric(job.incomplete_calls)} partial` : 'Recorded estimate'}</span></td></tr>)}</tbody></table></div>
      {query.isPending && <p className="p-6 text-sm text-slate-500">Loading job costs…</p>}{query.data && !query.data.items.length && <p className="p-6 text-sm text-slate-500">No matching jobs.</p>}</Card>
    {!!query.data?.unallocated?.calls && <p className="rounded-xl bg-amber-50 p-4 text-sm text-amber-900">{metric(query.data.unallocated.calls)} recorded attempts have no job attribution: {costMoney(query.data.unallocated.total_usd)}. They are tracked separately and not assigned to an arbitrary upload.</p>}
    <Pager page={page} size={25} total={query.data?.total ?? 0} busy={query.isFetching} onPage={setPage} />
  </>;
}
function JobCosts({ jobId }: { jobId: string }) {
  const [page, setPage] = useState(0);
  const query = useQuery({ queryKey: ['admin-cost', jobId, page], queryFn: ({ signal }) => getJobCost(jobId, page * 50, signal), refetchInterval: 10000 });
  const data = query.data;
  useEffect(() => { if (data && page * 50 >= data.total_leads && page > 0) setPage(Math.max(0, Math.ceil(data.total_leads / 50) - 1)); }, [data, page]);
  return <AdminLayout title={data?.file_name || 'Job cost detail'} description="All recorded usage for this upload, with individual lead drilldowns."><Link to="/admin/cost" className="text-sm text-brand-700 underline">← All job costs</Link><EstimateNotice />
    {query.isError && <AdminError retry={() => void query.refetch()} />}{!data ? <Card className="p-6">Loading cost detail…</Card> : <>
      <Summary value={data.summary} fx={data.usd_inr} asOf={data.fx_as_of} /><Prices pricing={data.pricing} /><Stages rows={data.by_stage} />
      {data.job_level.calls > 0 && <Card className="p-5"><h2 className="font-semibold text-slate-900">Job-level usage</h2><p className="mt-1 text-sm text-slate-600">{metric(data.job_level.calls)} import/chat or unassigned attempts · {costMoney(data.job_level.total_usd)} · {costMoney(data.job_level.total_inr, 'INR')}. Included in the job total; not arbitrarily assigned to a lead.</p></Card>}
      <Card className="overflow-hidden"><h2 className="border-b border-slate-100 p-5 font-semibold text-slate-900">Per-lead cost</h2><div className="overflow-x-auto"><table className="w-full"><thead className="bg-slate-50 text-slate-500"><tr>{['Row', 'Company / contact', 'Attempts', 'Input / output tokens', 'Bing requests', 'Known USD', 'INR', 'Incomplete'].map((label) => <th scope="col" key={label} className={cell}>{label}</th>)}</tr></thead><tbody className="divide-y divide-slate-100">{data.leads.map((lead) => <tr key={lead.lead_id} className="hover:bg-brand-50/30"><td className={cell}>{lead.row_index == null ? '—' : lead.row_index + 1}</td><td className={cell}><Link className="font-semibold text-brand-700 hover:underline" to={`/admin/cost/${jobId}/${lead.lead_id}`}>{lead.company_name || 'Unnamed company'}</Link><span className="block text-xs text-slate-500">{lead.contact_name || 'Unnamed contact'}</span></td><td className={cell}>{metric(lead.calls ?? 0)}</td><td className={cell}>{metric(lead.input_tokens ?? 0)} / {metric(lead.output_tokens ?? 0)}</td><td className={cell}>{metric(lead.search_requests ?? 0)}</td><td className={cell}>{recordedCost(lead)}</td><td className={cell}>{recordedCost(lead, 'INR')}</td><td className={cell}>{metric(lead.incomplete_calls ?? 0)}</td></tr>)}</tbody></table></div></Card>
      <Pager page={page} size={50} total={data.total_leads} busy={query.isFetching} onPage={setPage} />
    </>}</AdminLayout>;
}
function LeadCosts({ jobId, leadId }: { jobId: string; leadId: string }) {
  const [page, setPage] = useState(0);
  const query = useQuery({ queryKey: ['admin-cost', jobId, leadId, page], queryFn: ({ signal }) => getLeadCost(jobId, leadId, page * 50, signal), refetchInterval: 10000 });
  const data = query.data;
  useEffect(() => { if (data && page * 50 >= data.total_calls && page > 0) setPage(Math.max(0, Math.ceil(data.total_calls / 50) - 1)); }, [data, page]);
  return <AdminLayout title={data?.company_name || 'Lead usage detail'} description={data?.contact_name || 'Each recorded attempt, including failed requests and retries.'}><Link className="text-sm text-brand-700 underline" to={`/admin/cost/${jobId}`}>← Job cost detail</Link><EstimateNotice />
    {query.isError && <AdminError retry={() => void query.refetch()} />}{!data ? <Card className="p-6">Loading lead usage…</Card> : <>
      <Summary value={data.summary} fx={data.usd_inr} /><Stages rows={data.by_stage} />
      <Card className="overflow-hidden"><h2 className="border-b border-slate-100 p-5 font-semibold text-slate-900">Individual API attempts &amp; captured prices</h2><div className="divide-y divide-slate-100">{data.calls.map((call) => <details key={call.id} className="group p-5"><summary className="cursor-pointer list-none"><div className="flex flex-wrap items-center justify-between gap-3"><div><strong className="text-sm text-slate-900">{call.stage || 'Unspecified step'}</strong><span className="ml-2 rounded bg-slate-100 px-2 py-1 text-xs text-slate-600">{call.status || 'Legacy usage'}{call.error_code ? ` · ${call.error_code}` : ''}</span><p className="mt-1 text-xs text-slate-500">{eventTime(call.created_at)} · {call.billing_model || call.model || 'Model unknown'}</p></div><div className="text-right"><strong className="text-sm text-slate-900">{costMoney(call.cost_total_usd)}</strong><p className="text-xs text-brand-700">Expand usage &amp; rate snapshot ↓</p></div></div></summary>
        <dl className="mt-4 grid grid-cols-2 gap-3 rounded-xl bg-slate-50 p-4 text-sm sm:grid-cols-4">{[
          ['Input tokens', metric(call.prompt_tokens)], ['Cached input tokens', metric(call.cached_prompt_tokens)], ['Output tokens', metric(call.completion_tokens)], ['Provider Bing requests', metric(call.search_requests)],
          ['Input cost', costMoney(call.cost_input_usd)], ['Cached input cost', costMoney(call.cost_cached_usd)], ['Output cost', costMoney(call.cost_output_usd)], ['Bing grounding cost', costMoney(call.cost_search_usd)],
          ['Visible tool items (not billing)', metric(call.web_search_calls)], ['Request duration', call.duration_ms == null ? 'Not recorded' : `${(call.duration_ms / 1000).toFixed(2)}s`], ['Coverage', call.cost_complete ? 'Recorded estimate' : 'Incomplete / legacy'], ['Module', call.module || 'Unknown'],
        ].map(([label, value]) => <div key={label}><dt className="text-xs text-slate-500">{label}</dt><dd className="mt-1 break-words font-medium text-slate-800">{value}</dd></div>)}</dl>
        {call.provider_request_id && <p className="mt-3 break-all text-xs text-slate-500">Provider request: {call.provider_request_id}</p>}
        {call.pricing_snapshot ? <CapturedPrices snapshot={call.pricing_snapshot} /> : <p className="mt-3 text-xs text-amber-800">Historical call: no captured tariff or detailed billing metadata. The stage summary uses current reference rates where the model is identifiable.</p>}
      </details>)}{!data.calls.length && <p className="p-5 text-sm text-slate-500">No API attempts recorded for this lead. This does not establish zero historical charges.</p>}</div></Card>
      <Pager page={page} size={50} total={data.total_calls} busy={query.isFetching} onPage={setPage} />
    </>}</AdminLayout>;
}

function CapturedPrices({ snapshot }: { snapshot: Record<string, unknown> }) {
  const pricing = snapshot.pricing && typeof snapshot.pricing === 'object' ? snapshot.pricing as Partial<Pricing> : null;
  if (!pricing) return <p className="mt-3 text-xs text-amber-800">No tariff could be captured for this attempt.</p>;
  return <div className="mt-3 rounded-xl border border-slate-200 p-4"><h3 className="text-sm font-semibold text-slate-800">Tariff captured for this request</h3><dl className="mt-3 grid grid-cols-2 gap-3 text-xs sm:grid-cols-5">{[
    ['Input / 1M', pricing.input_per_million], ['Cached / 1M', pricing.cached_per_million], ['Output / 1M', pricing.output_per_million], ['Embedding / 1M', pricing.embedding_per_million], ['Bing / 1K requests', pricing.search_per_thousand],
  ].map(([label, value]) => <div key={String(label)}><dt className="text-slate-500">{label}</dt><dd className="mt-1 font-medium text-slate-800">{costMoney(value == null ? null : Number(value))}</dd></div>)}</dl><p className="mt-3 text-xs text-slate-500">{pricing.confirmed ? 'Rates confirmed by an administrator.' : 'Reference tariff; not yet confirmed against the Azure contract.'} Cached tokens are part of input tokens, not added again. Bing fees use provider-reported search requests.</p></div>;
}
