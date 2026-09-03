import { useQuery } from '@tanstack/react-query';
import { getDashboardStats } from '@/api/endpoints';

export interface DashboardStats {
  totalJobs: number;
  completedJobs: number;
  activeJobs: number;
  failedJobs: number;
  totalLeads: number;
  highPriority: number;
  mediumPriority: number;
  lowPriority: number;
  avgFinalScore: number;
}

const EMPTY: DashboardStats = {
  totalJobs: 0, completedJobs: 0, activeJobs: 0, failedJobs: 0,
  totalLeads: 0, highPriority: 0, mediumPriority: 0, lowPriority: 0,
  avgFinalScore: 0,
};

export function useDashboardStats(): { stats: DashboardStats; isLoading: boolean } {
  const { data, isLoading } = useQuery({
    queryKey: ['dashboard-stats'],
    queryFn: getDashboardStats,
    refetchInterval: 10_000,
    placeholderData: (prev) => prev,
    refetchOnWindowFocus: false,
  });

  const stats: DashboardStats = data ? {
    totalJobs: data.total_jobs,
    completedJobs: data.completed_jobs,
    activeJobs: data.active_jobs,
    failedJobs: data.failed_jobs,
    totalLeads: data.total_leads,
    highPriority: data.high_priority,
    mediumPriority: data.medium_priority,
    lowPriority: data.low_priority,
    avgFinalScore: data.avg_final_score,
  } : EMPTY;
  return { stats, isLoading };
}
