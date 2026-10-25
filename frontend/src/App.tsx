import { lazy, Suspense, useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '@/components/ui/Toast';
import { ProtectedRoute } from '@/components/ProtectedRoute';
import { isLoggedIn } from '@/lib/auth';
import { ExportProvider } from '@/components/ExportManager';
import { ApplicationErrorBoundary } from '@/components/ApplicationErrorBoundary';

const LoginPage = lazy(() => import('@/pages/LoginPage').then((m) => ({ default: m.LoginPage })));
const DashboardPage = lazy(() => import('@/pages/DashboardPage').then((m) => ({ default: m.DashboardPage })));
const UploadPage = lazy(() => import('@/pages/UploadPage').then((m) => ({ default: m.UploadPage })));
const JobDetailPage = lazy(() => import('@/pages/JobDetailPage').then((m) => ({ default: m.JobDetailPage })));
const LeadWorkspacePage = lazy(() => import('@/pages/LeadWorkspacePage').then((m) => ({ default: m.LeadWorkspacePage })));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 5_000,
    },
  },
});

export default function App() {
  useEffect(() => {
    const sessionChanged = (event: StorageEvent) => {
      if (event.storageArea !== localStorage || (event.key !== null && !['token', 'username'].includes(event.key))) return;
      void queryClient.cancelQueries();
      queryClient.clear();
      window.location.replace(isLoggedIn() ? '/dashboard' : '/login');
    };
    window.addEventListener('storage', sessionChanged);
    return () => window.removeEventListener('storage', sessionChanged);
  }, []);
  return (
    <QueryClientProvider client={queryClient}>
      <ApplicationErrorBoundary>
      <ToastProvider>
        <BrowserRouter>
          <ExportProvider>
          <Suspense fallback={<div className="min-h-screen bg-sky-gradient grid place-items-center text-sm text-slate-500">Loading workspace…</div>}>
          <Routes>
            <Route path="/login" element={isLoggedIn() ? <Navigate to="/dashboard" replace /> : <LoginPage />} />
            <Route path="/dashboard" element={<ProtectedRoute><DashboardPage /></ProtectedRoute>} />
            <Route path="/upload"    element={<ProtectedRoute><UploadPage /></ProtectedRoute>} />
            <Route path="/leads" element={<ProtectedRoute><LeadWorkspacePage /></ProtectedRoute>} />
            <Route path="/jobs/:jobId" element={<ProtectedRoute><JobDetailPage /></ProtectedRoute>} />
            <Route path="/" element={<Navigate to={isLoggedIn() ? '/dashboard' : '/login'} replace />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Routes>
          </Suspense>
          </ExportProvider>
        </BrowserRouter>
      </ToastProvider>
      </ApplicationErrorBoundary>
    </QueryClientProvider>
  );
}
