import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { isAxiosError } from 'axios';
import { AlertTriangle, Save, SlidersHorizontal } from 'lucide-react';
import { getAdminSettings, saveAdminSettings, refreshExchangeRate } from '@/api/admin';
import { AdminError, AdminLayout } from '@/components/AdminLayout';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useToast } from '@/components/ui/Toast';
import { eventTime } from '@/components/LiveIssuePanel';
import type { AdminSettings, SettingsSave } from '@/types/admin';

const RATE_FIELDS = [
  ['input_per_million', 'GPT-4.1 mini input · USD / 1M tokens'],
  ['cached_per_million', 'GPT-4.1 mini cached input · USD / 1M tokens'],
  ['output_per_million', 'GPT-4.1 mini output · USD / 1M tokens'],
  ['embedding_per_million', 'Ada-002 embedding input · USD / 1M tokens'],
  ['search_per_thousand', 'Bing grounding · USD / 1,000 requests'],
] as const;
type Rates = Record<typeof RATE_FIELDS[number][0], string>;
type Draft = { concurrency: number; version: number; rates: Rates; confirmed: boolean; fx: string };
function draftFrom(data: AdminSettings): Draft {
  return { concurrency: data.concurrency, version: data.version, confirmed: data.pricing.confirmed,
    rates: Object.fromEntries(RATE_FIELDS.map(([key]) => [key, String(data.pricing[key])])) as Rates,
    fx: data.usd_inr == null ? '' : String(data.usd_inr) };
}

export function AdminSettingsPage() {
  const query = useQuery({ queryKey: ['admin-settings'], queryFn: ({ signal }) => getAdminSettings(signal), refetchInterval: 5000 });
  const [draft, setDraft] = useState<Draft | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const sending = useRef(false);
  const qc = useQueryClient();
  const { toast } = useToast();
  useEffect(() => { if (query.data && !dirty) setDraft(draftFrom(query.data)); }, [query.data, dirty]);
  function update(change: Partial<Draft>) { setDirty(true); setDraft((old) => old ? { ...old, ...change } : old); setError(''); }
  function accepted(data: AdminSettings) { qc.setQueryData(['admin-settings'], data); setDraft(draftFrom(data)); setDirty(false); void qc.invalidateQueries({ queryKey: ['admin-cost'] }); }
  async function save() {
    if (!draft || sending.current) return;
    const rates = Object.fromEntries(RATE_FIELDS.map(([key]) => [key, Number(draft.rates[key])])) as NonNullable<SettingsSave['pricing']>;
    if (RATE_FIELDS.some(([key]) => draft.rates[key].trim() === '' || !Number.isFinite(rates[key]) || rates[key] < 0 || rates[key] > 10000)
        || (draft.fx !== '' && (!Number.isFinite(Number(draft.fx)) || Number(draft.fx) <= 0 || Number(draft.fx) > 10000))) {
      setError('Enter valid nonnegative rates; USD/INR must be positive.'); return;
    }
    sending.current = true; setBusy(true); setError('');
    const body: SettingsSave = { concurrency: draft.concurrency, version: draft.version,
      pricing: { ...rates, confirmed: draft.confirmed }, usd_inr: draft.fx === '' ? null : Number(draft.fx) };
    try { accepted(await saveAdminSettings(body)); toast('Settings saved. New lead starts use the saved limit.', 'success'); }
    catch (failure) {
      // Recover a successful SQL save whose HTTP response was interrupted.
      try {
        const latest = await getAdminSettings();
        if (latest.version > body.version && latest.concurrency === body.concurrency && latest.usd_inr === body.usd_inr
            && RATE_FIELDS.every(([key]) => latest.pricing[key] === rates[key]) && latest.pricing.confirmed === draft.confirmed) {
          accepted(latest); toast('Saved settings recovered after the interrupted response.', 'success'); return;
        }
      } catch { /* Keep the unsaved draft available. */ }
      const detail = isAxiosError(failure) ? failure.response?.data?.detail : null;
      setError(typeof detail === 'string' ? detail : 'Could not confirm the save. Your draft is retained; reload saved settings before retrying.');
    } finally { sending.current = false; setBusy(false); }
  }
  async function refreshFx() {
    if (dirty || sending.current) return;
    sending.current = true; setBusy(true); setError('');
    try { accepted(await refreshExchangeRate()); toast('Daily reference exchange rate updated.', 'success'); }
    catch { setError('Could not refresh the exchange rate. Retry or enter USD/INR manually.'); }
    finally { sending.current = false; setBusy(false); }
  }
  const capacity = query.data?.workers.reduce((sum, worker) => sum + worker.capacity, 0) ?? 0;
  return <AdminLayout title="Runtime settings" description="Database-backed controls for lead processing and cost estimates.">
    {query.isError && <AdminError retry={() => void query.refetch()} />}
    {!draft ? <Card className="p-6 text-slate-500">Loading settings…</Card> : <>
      <Card className="space-y-5 p-6">
        <div className="flex items-center gap-2"><SlidersHorizontal className="h-5 w-5 text-brand-600" /><h2 className="font-semibold text-slate-900">Concurrent lead processing</h2></div>
        <div className="flex items-center justify-between"><label htmlFor="worker-count" className="text-sm text-slate-700">Maximum active leads across all workers</label><output htmlFor="worker-count" className="rounded-xl bg-brand-50 px-5 py-2 text-2xl font-bold tabular-nums text-brand-700">{draft.concurrency}</output></div>
        <input id="worker-count" className="w-full accent-violet-600" type="range" min="1" max="30" step="1" value={draft.concurrency} disabled={busy} onChange={(event) => update({ concurrency: Number(event.target.value) })} />
        <div className="flex justify-between text-xs text-slate-500"><span>1 · lower load</span><span>30 · maximum</span></div>
        <div className={`flex gap-3 rounded-xl p-4 text-sm ${draft.concurrency >= 20 ? 'border border-amber-300 bg-amber-50 text-amber-900' : 'bg-sky-50 text-sky-900'}`}><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /><p>Higher concurrency can exceed Azure requests-per-minute or tokens-per-minute limits, create HTTP 429 errors, increase database load, and spend budget faster. Increase gradually. Saving changes new starts within a few seconds; leads already running finish safely.</p></div>
        <div className="grid gap-3 text-sm sm:grid-cols-3"><div className="rounded-xl bg-slate-50 p-3">Saved limit <strong className="float-right">{query.data?.concurrency}</strong></div><div className="rounded-xl bg-slate-50 p-3">Active leads <strong className="float-right">{query.data?.active ?? 'Unavailable'}</strong></div><div className="rounded-xl bg-slate-50 p-3">Online execution slots <strong className="float-right">{capacity}</strong></div></div>
        {capacity < draft.concurrency && <p role="status" className="text-sm text-amber-800">Online workers currently provide {capacity} execution slots. Start the updated 30-slot qualification worker to use the full slider range; a solo worker processes one lead at a time.</p>}
        {!query.data?.redis_available && <p role="alert" className="text-sm text-rose-700">Redis is unavailable. The SQL setting can be saved, but new lead starts wait until coordination recovers.</p>}
        {!!query.data?.cooldown_seconds && <p role="status" className="text-sm text-amber-900">Azure rate-limit cooldown: about {query.data.cooldown_seconds}s remaining. New leads wait; saved stages are retained.</p>}
        {!!query.data?.telemetry_pending && <p className="text-xs text-slate-500">{query.data.telemetry_pending.toLocaleString()} usage/log records awaiting persistence.</p>}
      </Card>
      <Card className="space-y-4 p-6"><h2 className="font-semibold text-slate-900">Current Azure models &amp; reference prices</h2>
        <p className="text-sm text-slate-600">GPT-4.1 mini · deployment <strong>{query.data?.deployment}</strong><br />text-embedding-ada-002 · deployment <strong>{query.data?.embedding_deployment}</strong></p>
        <p className="text-xs text-amber-900">Confirm rates for your Azure region, deployment tier and contract. Reports are estimates, exclude taxes and other Azure infrastructure, and preserve captured prices on new calls. Price edits affect future calls and estimates for legacy records only.</p>
        <div className="grid gap-4 sm:grid-cols-2">{RATE_FIELDS.map(([key, label]) => <label key={key} className="space-y-1 text-sm text-slate-700"><span>{label}</span><input className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2" type="number" min="0" max="10000" step="0.000001" value={draft.rates[key]} disabled={busy} onChange={(event) => update({ rates: { ...draft.rates, [key]: event.target.value } })} /></label>)}</div>
        <label className="flex items-center gap-2 text-sm text-slate-700"><input type="checkbox" checked={draft.confirmed} disabled={busy} onChange={(event) => update({ confirmed: event.target.checked })} />I have confirmed these rates against our Azure pricing.</label>
        <div className="flex gap-4 text-xs"><a className="text-brand-700 underline" href="https://azure.microsoft.com/en-us/pricing/details/azure-openai/" target="_blank" rel="noreferrer">Azure token pricing</a><a className="text-brand-700 underline" href="https://www.microsoft.com/en-us/bing/apis" target="_blank" rel="noreferrer">Bing grounding pricing</a></div>
      </Card>
      <Card className="space-y-4 p-6"><h2 className="font-semibold text-slate-900">USD → INR reference conversion</h2><div className="flex flex-wrap items-end gap-3"><label className="space-y-1 text-sm text-slate-700"><span>INR per 1 USD</span><input className="block rounded-lg border border-slate-200 px-3 py-2" type="number" min="0.000001" max="10000" step="0.000001" placeholder="Fetch a rate or enter manually" value={draft.fx} disabled={busy} onChange={(event) => update({ fx: event.target.value })} /></label><Button variant="outline" disabled={busy || dirty} onClick={() => void refreshFx()}>Refresh daily rate</Button></div>
        <p className="text-xs text-slate-500">{query.data?.fx_as_of ? `Reference date: ${eventTime(query.data.fx_as_of)} · ${query.data.fx_source || 'Configured rate'}` : 'No rate configured. The cost page attempts a daily reference-rate fetch; INR stays unavailable if it cannot be obtained.'} {dirty && 'Save or reset changes before refreshing the rate.'}</p></Card>
      {error && <div role="alert" className="rounded-xl bg-rose-50 p-4 text-sm text-rose-800">{error}</div>}
      <div className="flex flex-wrap items-center gap-3"><Button disabled={busy || !dirty} icon={<Save className="h-4 w-4" />} onClick={() => void save()}>{busy ? 'Saving…' : 'Save settings'}</Button><Button variant="outline" disabled={busy} onClick={async () => { const result = await query.refetch(); if (result.data) { accepted(result.data); setError(''); } }}>Reload saved settings</Button><span className="text-xs text-slate-500">{query.data?.updated_at ? `Last saved ${eventTime(query.data.updated_at)} by ${query.data.updated_by || 'system'}` : 'Default limit: 5. The legacy MAX_WORKERS environment value is unused.'}</span></div>
    </>}
  </AdminLayout>;
}
