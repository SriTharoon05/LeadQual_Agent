import { motion } from 'framer-motion';
import { Link } from 'react-router-dom';
import {
  Briefcase, CheckCircle2, Loader2, Users, Flame, TrendingUp,
  Upload, FileSpreadsheet, FileText, Trash2, Eye,
} from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { TopBar } from '@/components/TopBar';
import { JobControls } from '@/components/JobControls';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge, StatusBadge } from '@/components/ui/Badge';
import { CardSkeleton, TableSkeleton } from '@/components/ui/Skeleton';
import { useDashboardStats } from '@/hooks/useDashboardStats';
import { getJobs, deleteJob } from '@/api/endpoints';
import { useExports } from '@/components/ExportManager';
import { useToast } from '@/components/ui/Toast';
import { fmtDate, numVal, safeArr } from '@/lib/format';
import type { JobSummary } from '@/types';
import { useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { invalidateLeadData } from '@/lib/leadCache';

export function DashboardPage() {
  const { stats, isLoading } = useDashboardStats();
  const { toast } = useToast();
  const qc = useQueryClient();
  const exports = useExports();

  const { data: jobs = [], isLoading: jobsLoading } = useQuery({
    queryKey: ['jobs'],
    queryFn: getJobs,
    refetchInterval: 8_000,
    placeholderData: (prev) => prev,
    refetchOnWindowFocus: false,
  });

  const handleDelete = async (jobId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm('Delete this job and all its results?')) return;
    try {
      await deleteJob(jobId);
      toast('Job deleted', 'success');
      qc.invalidateQueries({ queryKey: ['jobs'] });
      qc.invalidateQueries({ queryKey: ['dashboard-stats'] });
      void invalidateLeadData(qc);
    } catch {
      toast('Failed to delete job', 'error');
    }
  };

  const handleExport = (jobId: string, format: 'excel' | 'csv', e: React.MouseEvent) => {
    e.stopPropagation();
    exports.start(jobId, format);
  };

  const kpis = [
    { label: 'Total Jobs',      value: stats.totalJobs,         icon: Briefcase,    color: 'from-brand-500 to-brand-600' },
    { label: 'Jobs Completed',  value: stats.completedJobs,     icon: CheckCircle2, color: 'from-emerald-500 to-emerald-600' },
    { label: 'Running / Queued',value: stats.activeJobs,        icon: Loader2,      color: 'from-sky-500 to-sky-600', spin: stats.activeJobs > 0 },
    { label: 'Total Leads',     value: stats.totalLeads,        icon: Users,        color: 'from-indigo-500 to-indigo-600' },
    { label: 'High-Priority',   value: stats.highPriority,      icon: Flame,        color: 'from-rose-500 to-rose-600' },
    { label: 'Avg Final Score', value: stats.avgFinalScore.toFixed(1), icon: TrendingUp, color: 'from-accent-500 to-accent-600' },
  ];

  return (
    <div className="min-h-screen bg-sky-gradient">
      <TopBar />
      <main className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8 py-8 space-y-8">
        {/* Header */}
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold text-slate-800">Dashboard</h1>
            <p className="text-sm text-slate-500 mt-1">Overview of your lead qualification pipeline</p>
          </div>
          <div className="flex gap-2"><Link to="/upload">
            <Button icon={<Upload className="h-4 w-4" />}>New Upload</Button>
          </Link></div>
        </div>

        {/* KPI cards */}
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
          {isLoading
            ? Array.from({ length: 6 }).map((_, i) => <CardSkeleton key={i} />)
            : kpis.map((k, i) => {
                const Icon = k.icon;
                return (
                  <motion.div
                    key={k.label}
                    initial={{ opacity: 0, y: 12 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: i * 0.05 }}
                  >
                    <Card hover className="p-5">
                      <div className={`inline-flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br ${k.color} shadow-sm`}>
                        <Icon className={`h-5 w-5 text-white ${k.spin ? 'animate-spin' : ''}`} />
                      </div>
                      <p className="mt-3 text-2xl font-bold text-slate-800">{k.value}</p>
                      <p className="text-xs font-medium text-slate-500 mt-0.5">{k.label}</p>
                    </Card>
                  </motion.div>
                );
              })}
        </div>


        {/* Jobs table */}
        <Card className="overflow-hidden">
          <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
            <h3 className="text-sm font-semibold text-slate-700">Recent Jobs</h3>
            <Badge color="brand">{safeArr(jobs).length} total</Badge>
          </div>
          {jobsLoading ? (
            <div className="p-6"><TableSkeleton /></div>
          ) : jobs.length === 0 ? (
            <EmptyState
              icon={<Briefcase className="h-10 w-10" />}
              title="No jobs yet"
              subtitle="Upload a lead file to get started"
              action={<Link to="/upload"><Button icon={<Upload className="h-4 w-4" />}>Upload File</Button></Link>}
            />
          ) : (
            <JobsTable jobs={jobs} onDelete={handleDelete} onExport={handleExport} isExporting={exports.isBusy} />
          )}
        </Card>
      </main>
    </div>
  );
}

function JobsTable({ jobs, onDelete, onExport, isExporting }: {
  jobs: JobSummary[];
  onDelete: (id: string, e: React.MouseEvent) => void;
  onExport: (id: string, fmt: 'excel' | 'csv', e: React.MouseEvent) => void;
  isExporting: (id: string, fmt: 'excel' | 'csv') => boolean;
  
}) {
  const navigate = useNavigate();
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-100 bg-slate-50/50">
            <th className="px-6 py-3 text-left font-semibold text-slate-600">File</th>
            <th className="px-4 py-3 text-left font-semibold text-slate-600">Status</th>
            <th className="px-4 py-3 text-left font-semibold text-slate-600 min-w-[160px]">Progress</th>
            <th className="px-4 py-3 text-left font-semibold text-slate-600">Created</th>
            <th className="px-4 py-3 text-right font-semibold text-slate-600">Actions</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-50">
          {jobs.map((job) => {
            const pct = Math.max(0, Math.min(100, numVal(job.progress_pct)));
            return (
              <tr
                key={job.job_id}
                className="hover:bg-brand-50/40 transition-colors cursor-pointer group"
                // onClick={() => (window.location.href = `/jobs/${job.job_id}`)}
                onClick={() => navigate(`/jobs/${job.job_id}`)}
              >
                <td className="px-6 py-3">
                  <div className="font-medium text-slate-800 truncate max-w-[200px]">{job.file_name ?? job.job_id}</div>
                  <div className="text-xs text-slate-400">{job.file_type ?? '—'}</div>
                </td>
                <td className="px-4 py-3"><StatusBadge status={job.status} /></td>
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <div className="flex-1 h-2 rounded-full bg-slate-100 overflow-hidden">
                      <div
                        className={`h-full rounded-full transition-all ${job.status === 'failed' ? 'bg-rose-400' : job.status === 'completed' ? 'bg-emerald-400' : 'bg-brand-400'}`}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <span className="text-xs text-slate-500 tabular-nums w-10 text-right">
                      {job.progress_pct != null ? `${pct.toFixed(0)}%` : '—'}
                    </span>
                  </div>
                  <div className="text-xs text-slate-400 mt-0.5">
                    {job.processed_rows ?? 0} / {job.total_rows ?? 0} rows
                  </div>
                </td>
                <td className="px-4 py-3 text-slate-500 whitespace-nowrap">{fmtDate(job.created_at)}</td>
                <td className="px-4 py-3">
                  <div className="flex items-center justify-end gap-1">
                    <JobControls jobId={job.job_id} status={job.status} compact />
                    <Link to={`/jobs/${job.job_id}`} onClick={(e) => e.stopPropagation()}>
                      <Button size="sm" variant="ghost" icon={<Eye className="h-3.5 w-3.5" />}>View</Button>
                    </Link>
                    <button disabled={job.status !== 'completed' || isExporting(job.job_id, 'excel')} onClick={(e) => onExport(job.job_id, 'excel', e)} title="Export all rows to Excel" aria-label="Export all rows to Excel" className="rounded-lg p-2 text-emerald-600 hover:bg-emerald-50 disabled:opacity-30 disabled:cursor-not-allowed transition-colors">
                      {isExporting(job.job_id, 'excel') ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileSpreadsheet className="h-4 w-4" />}
                    </button>
                    <button disabled={job.status !== 'completed' || isExporting(job.job_id, 'csv')} onClick={(e) => onExport(job.job_id, 'csv', e)} title="Export all rows to CSV" aria-label="Export all rows to CSV" className="rounded-lg p-2 text-sky-600 hover:bg-sky-50 disabled:opacity-30 disabled:cursor-not-allowed transition-colors">
                      {isExporting(job.job_id, 'csv') ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileText className="h-4 w-4" />}
                    </button>
                    <button disabled={['running', 'paused', 'importing'].includes(job.status)} onClick={(e) => onDelete(job.job_id, e)} title="Delete" className="rounded-lg p-2 text-rose-500 hover:bg-rose-50 disabled:opacity-30 disabled:cursor-not-allowed transition-colors">
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function EmptyState({ icon, title, subtitle, action }: {
  icon: React.ReactNode;
  title: string;
  subtitle: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-16 px-4 text-center">
      <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-brand-50 text-brand-300 mb-4">{icon}</div>
      <h3 className="text-base font-semibold text-slate-700">{title}</h3>
      <p className="text-sm text-slate-400 mt-1 mb-4">{subtitle}</p>
      {action}
    </div>
  );
}

// re-export for use elsewhere
