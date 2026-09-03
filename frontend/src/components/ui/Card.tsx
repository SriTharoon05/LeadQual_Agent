import type { ReactNode } from 'react';

export function Card({ children, className = '', hover = false }: {
  children: ReactNode;
  className?: string;
  hover?: boolean;
}) {
  return (
    <div className={`rounded-2xl bg-white shadow-card ring-1 ring-slate-100 ${hover ? 'transition-shadow hover:shadow-card-hover' : ''} ${className}`}>
      {children}
    </div>
  );
}
