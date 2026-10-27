import type { ReactNode } from 'react';
import type { JobStatus, Priority } from '@/types';

export function Badge({ children, color = 'neutral', className = '' }: {
  children: ReactNode;
  color?: 'neutral' | 'brand' | 'success' | 'warning' | 'danger' | 'info';
  className?: string;
}) {
  const colors = {
    neutral: 'bg-slate-100 text-slate-600 ring-slate-200',
    brand:   'bg-brand-100 text-brand-700 ring-brand-200',
    success: 'bg-emerald-100 text-emerald-700 ring-emerald-200',
    warning: 'bg-amber-100 text-amber-700 ring-amber-200',
    danger:  'bg-rose-100 text-rose-700 ring-rose-200',
    info:    'bg-sky-100 text-sky-700 ring-sky-200',
  };
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold ring-1 ring-inset ${colors[color]} ${className}`}>
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: JobStatus | null | undefined }) {
  if (!status) return <Badge>—</Badge>;

  const map: Record<JobStatus, { color: Parameters<typeof Badge>[0]['color']; label: string; pulse?: boolean }> = {
    queued:        { color: 'neutral', label: 'Queued' },
    running:       { color: 'info',    label: 'Running',        pulse: true },
    completed:     { color: 'success', label: 'Completed' },
    failed:        { color: 'danger',  label: 'Failed' },
    importing:     { color: 'warning', label: 'Importing',      pulse: true },
    import_failed: { color: 'danger',  label: 'Import Failed' },
    paused:        { color: 'warning', label: 'Paused' },
  };

  const cfg = map[status] ?? { color: 'neutral' as const, label: status };

  return (
    <Badge color={cfg.color}>
      {cfg.pulse && <span className="h-1.5 w-1.5 rounded-full bg-sky-500 animate-pulse" />}
      {cfg.label}
    </Badge>
  );
}

export function PriorityBadge({ priority }: { priority: Priority | null | undefined }) {
  if (!priority) return <Badge>—</Badge>;
  const map: Record<Priority, { color: Parameters<typeof Badge>[0]['color']; label: string }> = {
    High:   { color: 'danger',  label: 'High' },
    Medium: { color: 'warning', label: 'Medium' },
    Low:    { color: 'info',    label: 'Low' },
  };
  const cfg = map[priority];
  return <Badge color={cfg.color}>{cfg.label}</Badge>;
}

export function ScoreChip({ score }: { score: number | null | undefined }) {
  if (score === null || score === undefined || Number.isNaN(score)) {
    return <span className="text-slate-400">—</span>;
  }
  const n = Number(score);
  const color = n >= 75 ? 'text-emerald-600 bg-emerald-50 ring-emerald-200'
              : n >= 50 ? 'text-amber-600 bg-amber-50 ring-amber-200'
              : 'text-rose-600 bg-rose-50 ring-rose-200';
  return (
    <span className={`inline-flex items-center justify-center rounded-lg px-2 py-0.5 text-xs font-bold ring-1 ring-inset ${color}`}>
      {n.toFixed(1)}
    </span>
  );
}
