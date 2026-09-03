/* eslint-disable react-refresh/only-export-components -- provider and hook intentionally share this small module */
import { createContext, useContext, useCallback, useState, useEffect, type ReactNode } from 'react';
import { CheckCircle2, XCircle, AlertTriangle, Info, X } from 'lucide-react';

type ToastType = 'success' | 'error' | 'warning' | 'info';

interface Toast {
  id: number;
  type: ToastType;
  message: string;
}

interface ToastContextValue {
  toast: (message: string, type?: ToastType) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

let toastId = 0;
const listeners = new Set<(t: Toast) => void>();

export const pushToast = (message: string, type: ToastType = 'info') => {
  const t = { id: ++toastId, type, message };
  listeners.forEach((l) => l(t));
};

export const useToast = (): ToastContextValue => {
  const ctx = useContext(ToastContext);
  return ctx ?? { toast: (m, t = 'info') => pushToast(m, t) };
};

const styles: Record<ToastType, { icon: typeof Info; ring: string; iconColor: string }> = {
  success: { icon: CheckCircle2, ring: 'ring-emerald-200', iconColor: 'text-emerald-500' },
  error:   { icon: XCircle,      ring: 'ring-rose-200',    iconColor: 'text-rose-500' },
  warning: { icon: AlertTriangle, ring: 'ring-amber-200',  iconColor: 'text-amber-500' },
  info:    { icon: Info,          ring: 'ring-brand-200',  iconColor: 'text-brand-500' },
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const remove = useCallback((id: number) => setToasts((prev) => prev.filter((t) => t.id !== id)), []);

  useEffect(() => {
    const listener = (t: Toast) => {
      setToasts((prev) => [...prev, t]);
      setTimeout(() => remove(t.id), 5000);
    };
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  }, [remove]);

  return (
    <ToastContext.Provider value={{ toast: (m, t = 'info') => pushToast(m, t) }}>
      {children}
      <div className="fixed bottom-6 right-6 z-[100] flex flex-col gap-3 pointer-events-none">
        {toasts.map((t) => {
          const s = styles[t.type];
          const Icon = s.icon;
          return (
            <div
              key={t.id}
              className={`pointer-events-auto flex items-start gap-3 rounded-xl bg-white shadow-card-hover ring-1 ${s.ring} px-4 py-3 max-w-sm animate-slide-up`}
            >
              <Icon className={`h-5 w-5 shrink-0 mt-0.5 ${s.iconColor}`} />
              <p className="text-sm text-slate-700 flex-1 leading-snug">{t.message}</p>
              <button onClick={() => remove(t.id)} className="text-slate-400 hover:text-slate-600 transition-colors">
                <X className="h-4 w-4" />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}
