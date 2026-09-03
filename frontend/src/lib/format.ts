// ── Null-safe display helpers ────────────────────────────────────────────────
export const dash = <T,>(v: T | null | undefined): T | string => (v === null || v === undefined ? '—' : v);

export const num = (v: number | null | undefined, decimals = 1): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Number(v).toFixed(decimals);
};

export const safeArr = <T,>(v: T[] | null | undefined): T[] => (Array.isArray(v) ? v : []);

export const safeLen = <T,>(v: T[] | null | undefined): number => (Array.isArray(v) ? v.length : 0);

export const numVal = (v: number | null | undefined): number => (v === null || v === undefined || Number.isNaN(v) ? 0 : Number(v));

// ── Date formatting ─────────────────────────────────────────────────────────
export const fmtDate = (v: string | null | undefined): string => {
  if (!v) return '—';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
};

export const fmtDateShort = (v: string | null | undefined): string => {
  if (!v) return '—';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
};

// ── File size ───────────────────────────────────────────────────────────────
export const fmtBytes = (bytes: number): string => {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
};

// ── File validation ─────────────────────────────────────────────────────────
export const ALLOWED_EXT = ['.xlsx', '.xls', '.xlsm', '.csv'];

export const isValidFile = (name: string): boolean => {
  const lower = name.toLowerCase();
  return ALLOWED_EXT.some((ext) => lower.endsWith(ext));
};
