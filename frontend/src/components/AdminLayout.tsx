import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { TopBar } from '@/components/TopBar';

export function AdminLayout({ title, description, children }: { title: string; description: string; children: ReactNode }) {
  return <div className="min-h-screen bg-sky-gradient"><TopBar /><main className="mx-auto max-w-7xl space-y-6 px-4 py-8 sm:px-6">
    <div><Link to="/dashboard" className="text-sm text-brand-700 hover:underline">← Dashboard</Link><h1 className="mt-3 text-2xl font-bold text-slate-900">{title}</h1><p className="mt-1 text-sm text-slate-600">{description}</p></div>
    {children}
  </main></div>;
}

export function AdminError({ retry, message = 'Could not load this data.' }: { retry: () => void; message?: string }) {
  return <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800">{message} <button className="font-semibold underline" onClick={retry}>Retry</button></div>;
}

export function costMoney(value: number | null | undefined, currency = 'USD') {
  return value != null && Number.isFinite(Number(value)) ? new Intl.NumberFormat(undefined, { style: 'currency', currency, minimumFractionDigits: 2, maximumFractionDigits: currency === 'USD' ? 8 : 6 }).format(Number(value)) : 'Not available';
}

export function metric(value: number | null | undefined) {
  return value == null ? 'Not reported' : Number(value).toLocaleString();
}
