import { Link } from 'react-router-dom';
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
