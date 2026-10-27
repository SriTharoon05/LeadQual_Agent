import { useRef } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { isAxiosError } from 'axios';
import { Pause, Play } from 'lucide-react';
import { pauseJob, resumeJob } from '@/api/endpoints';
import { Button } from '@/components/ui/Button';
import { useToast } from '@/components/ui/Toast';
import type { JobStatus } from '@/types';

export function JobControls({ jobId, status, compact = false }: {
  jobId: string; status: JobStatus | undefined; compact?: boolean;
}) {
  const qc = useQueryClient();
  const { toast } = useToast();
  const busy = useRef(false);
  const action = useMutation({
    mutationFn: () => status === 'paused' ? resumeJob(jobId) : pauseJob(jobId),
    onSuccess: async (job) => {
      await qc.cancelQueries({ queryKey: ['job-status', jobId] });
      qc.setQueryData(['job-status', jobId], job);
      toast(job.status === 'paused' ? 'Job paused. In-flight leads may still finish.' : 'Job resumed.', 'success');
    },
    onError: (error) => {
      const detail = isAxiosError(error) ? error.response?.data?.detail : undefined;
      toast(typeof detail === 'string' ? detail : 'Could not confirm the change. Refresh job status and try again.', 'error');
    },
    onSettled: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['jobs'] }),
        qc.invalidateQueries({ queryKey: ['job-status', jobId] }),
        qc.invalidateQueries({ queryKey: ['dashboard-stats'] }),
      ]);
      busy.current = false;
    },
  });
  if (status !== 'running' && status !== 'paused') return null;
  const paused = status === 'paused';
  return (
    <div onClick={(event) => event.stopPropagation()} className="space-y-2">
      <Button size="sm" variant="outline" loading={action.isPending}
        icon={paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
        onClick={() => { if (!busy.current) { busy.current = true; action.mutate(); } }}>
        {paused ? 'Resume job' : 'Pause job'}
      </Button>
      {!compact && <p className="text-xs text-slate-500 max-w-sm">
        {paused ? 'Paused. In-flight leads may finish; new leads will wait. Resume keeps all saved progress.'
          : 'Pause stops new leads from starting. Leads already in progress finish safely.'}
      </p>}
    </div>
  );
}
