import { Globe2, ShieldCheck } from 'lucide-react';
import { useState } from 'react';
import { fmtDate } from '@/lib/format';
import type { LeadResult } from '@/types';

function sourceUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function SourceGlobe({ url, title, excerpt }: { url: string; title?: string; excerpt?: string }) {
  const href = sourceUrl(url);
  if (!href) return null;
  const name = title || new URL(href).hostname;
  return <a href={href} target="_blank" rel="noopener noreferrer"
    title={`${name}${excerpt ? ` — ${excerpt}` : ''}`}
    aria-label={`Open evidence: ${name}${excerpt ? `. ${excerpt}` : ''}`}
    className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-sky-50 text-sky-700 ring-1 ring-sky-200 hover:bg-sky-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-600">
    <Globe2 className="h-4 w-4" />
  </a>;
}

const verdicts = {
  match: ['Match', 'bg-emerald-50 text-emerald-800 ring-emerald-200'],
  mismatch: ['Mismatch', 'bg-rose-50 text-rose-800 ring-rose-200'],
  unclear: ['Unclear', 'bg-amber-50 text-amber-800 ring-amber-200'],
  not_specified: ['Not requested', 'bg-slate-100 text-slate-600 ring-slate-200'],
};

function ProofSources({ sources }: { sources: { url: string; title: string; excerpt: string }[] }) {
  const [expanded, setExpanded] = useState(false);
  const unique = [...new Map(sources.map((source) => [source.url, source] as const)).values()];
  return <span className="inline-flex flex-wrap items-center gap-1.5">
    {(expanded ? unique : unique.slice(0, 2)).map((source) => <SourceGlobe key={source.url} {...source} />)}
    {unique.length > 2 && <button type="button" onClick={() => setExpanded((value) => !value)} className="text-xs text-sky-700 underline">{expanded ? 'See less' : `See ${unique.length - 2} more sources`}</button>}
  </span>;
}

export function IcpEvidence({ lead }: { lead: LeadResult }) {
  return <div className="space-y-4">
    <div className="rounded-lg border border-brand-100 bg-white p-3">
      <p className="text-xs font-semibold uppercase tracking-wide text-brand-700">ICP input</p>
      <p className="mt-1 whitespace-pre-wrap text-sm text-slate-700">{lead.icp_input || 'ICP input unavailable for this result.'}</p>
    </div>
    <div>
      <h5 className="flex items-center gap-2 font-semibold text-slate-800"><ShieldCheck className="h-4 w-4 text-brand-500" />ICP validation proofs</h5>
      <p className="mt-1 text-xs text-slate-500">ICP fit, contact availability, and growth evidence. Open a globe to inspect a source. Contact checks establish availability, not guaranteed delivery.</p>
      {lead.evidence_checked_at && <p className="mt-1 text-xs text-slate-500">Evidence checked: {fmtDate(lead.evidence_checked_at)}{lead.reuse_kind ? ' · Reused evidence retains its original date' : ''}</p>}
    </div>
    {!lead.icp_proofs?.some((proof) => ['employee_count', 'location', 'annual_revenue', 'industry', 'seniority', 'employment'].includes(proof.field)) && <p className="rounded-lg bg-amber-50 p-3 text-sm text-amber-900">This earlier result has no complete company/contact ICP proofs. Refresh its evidence before treating the ICP as confirmed.</p>}
    {!lead.icp_proofs?.length ? null :
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {lead.icp_proofs.map((proof) => {
          const [label, color] = verdicts[proof.verdict] || verdicts.unclear;
          const finding = proof.field === 'employment'
            ? ({ current: 'Currently employed', former: 'No longer employed', unclear: 'Unclear' }[proof.value] || proof.value)
            : proof.value;
          return <article key={proof.field} className="rounded-xl border border-slate-200 bg-white p-4">
            <div className="flex items-start justify-between gap-2">
              <h6 className="text-sm font-semibold text-slate-800">{proof.label}</h6>
              <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ${color}`}>{label}</span>
            </div>
            <p className="mt-3 text-xs text-slate-500">ICP requirement</p>
            <p className="mt-0.5 text-sm text-slate-600">{proof.requirement}</p>
            <p className="mt-3 text-xs font-medium text-slate-500">{proof.field === 'contact_method' ? 'Recorded contact check' : proof.field === 'growth' ? 'Sourced growth check' : 'AI ICP check'}</p>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <p className="text-sm font-semibold text-slate-800">{finding}</p>
              <ProofSources sources={proof.sources} />
            </div>
            {proof.status === 'conflicting' && <p className="mt-1 text-xs font-medium text-amber-700">Sources disagree</p>}
            {proof.explanation && <p className="mt-2 text-xs leading-relaxed text-slate-500">{proof.explanation}</p>}
            {proof.required === false && proof.field !== 'growth' && proof.verdict !== 'not_specified' && <p className="mt-1 text-xs text-slate-500">Preference · does not disqualify a lead</p>}
            {proof.methods?.map((method, index) => <div key={`${method.kind}-${method.value}-${index}`} className="mt-3 rounded-lg border border-slate-100 bg-slate-50 p-3 text-xs">
              <p className="break-all font-semibold text-slate-700">{method.kind === 'email' ? 'Email' : 'Phone'} · {method.value}</p>
              <p className="mt-1 leading-relaxed text-slate-600">{method.explanation}</p>
              <p className="mt-1 text-slate-500">{method.kind === 'email' ? 'Inbox delivery' : 'Call reachability'}: unknown{method.checked_at ? ` · Checked ${fmtDate(method.checked_at)}` : ' · Original check details unavailable'}</p>
              {!!method.observations?.length && <details className="mt-2 text-slate-600"><summary className="cursor-pointer font-medium">Inspect recorded check</summary><ul className="mt-2 space-y-1 break-words">{method.observations.map((observation, item) => <li key={item}>
                <span className="font-medium">{observation.type}</span>{observation.domain ? ` · ${observation.domain}` : ''}{observation.e164 ? ` · ${observation.e164}` : ''}{observation.records?.length ? ` · ${observation.records.join('; ')}` : ''}{observation.code ? ` · response ${observation.code}` : ''}{observation.response ? ` · ${observation.response}` : ''}{observation.format_valid != null ? ` · format ${observation.format_valid ? 'valid' : 'not confirmed'}` : ''}
              </li>)}</ul><p className="mt-2 text-[11px]">DNS links show live records; these observations preserve what this check returned.</p></details>}
            </div>)}
            {proof.reasoning && <p className="mt-2 text-xs leading-relaxed text-slate-600">{proof.reasoning}</p>}
          </article>;
        })}
      </div>}
    {lead.reasoning && <div className="border-t border-slate-200 pt-3"><p className="text-xs font-semibold text-slate-500">Qualification summary</p><p className="mt-1 text-sm leading-relaxed text-slate-600">{lead.reasoning}</p></div>}
  </div>;
}
