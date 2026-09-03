import { Navigate } from 'react-router-dom';
import type { ReactNode } from 'react';
import { isLoggedIn } from '@/lib/auth';

export function ProtectedRoute({ children }: { children: ReactNode }) {
  if (!isLoggedIn()) return <Navigate to="/login" replace />;
  return <>{children}</>;
}
