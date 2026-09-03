import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useLocation } from 'react-router-dom';
import { Sparkles, LogOut, Activity, LayoutDashboard, Upload } from 'lucide-react';
import { getHealth } from '@/api/endpoints';
import { clearAuth, getUser } from '@/lib/auth';
import { useToast } from '@/components/ui/Toast';

export function TopBar() {
  const location = useLocation();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const username = getUser();

  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    refetchInterval: 30_000,
    refetchOnWindowFocus: false,
    retry: 1,
  });

  const healthy = health?.status === 'ok' || health?.status === 'healthy';

  const handleLogout = () => {
    clearAuth();
    queryClient.clear();

    toast('Signed out successfully', 'info');
    window.location.replace('/login');
  };

  const navLink = (to: string, label: string, icon: React.ReactNode) => {
    const active = location.pathname === to || (to === '/dashboard' && location.pathname.startsWith('/jobs'));
    return (
      <Link
        to={to}
        className={`inline-flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
          active ? 'bg-brand-100 text-brand-700' : 'text-slate-600 hover:bg-brand-50 hover:text-brand-700'
        }`}
      >
        {icon}
        {label}
      </Link>
    );
  };

  return (
    <header className="sticky top-0 z-40 border-b border-slate-200/70 bg-white/80 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-4 sm:px-6 lg:px-8">
        <div className="flex items-center gap-8">
          <Link to="/dashboard" className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-brand-gradient shadow-brand">
              <Sparkles className="h-5 w-5 text-white" />
            </div>
            <span className="hidden sm:block text-lg font-bold text-slate-800">LeadQual<span className="text-brand-600">AI</span></span>
          </Link>
          <nav className="hidden md:flex items-center gap-1">
            {navLink('/dashboard', 'Dashboard', <LayoutDashboard className="h-4 w-4" />)}
            {navLink('/upload', 'Upload', <Upload className="h-4 w-4" />)}
          </nav>
        </div>

        <div className="flex items-center gap-3">
          {/* Health pill */}
          <div
            className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold ring-1 ring-inset ${
              healthy
                ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                : 'bg-rose-50 text-rose-700 ring-rose-200'
            }`}
            title={healthy ? 'System healthy' : 'System unreachable'}
          >
            <Activity className={`h-3.5 w-3.5 ${healthy ? '' : 'animate-pulse'}`} />
            <span className="hidden sm:inline">System {healthy ? 'healthy' : 'unreachable'}</span>
            <span className="sm:hidden">{healthy ? 'OK' : 'Down'}</span>
          </div>

          {username && (
            <span className="hidden sm:block text-sm text-slate-500">
              <span className="text-slate-400">Hi,</span> <span className="font-medium text-slate-700">{username}</span>
            </span>
          )}

          <button
            onClick={handleLogout}
            className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium text-slate-600 hover:bg-rose-50 hover:text-rose-600 transition-colors"
            aria-label="Logout"
          >
            <LogOut className="h-4 w-4" />
            <span className="hidden sm:inline">Logout</span>
          </button>
        </div>
      </div>
    </header>
  );
}
