import { useId } from 'react';
import type { ProcessingMode } from '@/types';

export function ProcessingModeChoice({ value, onChange, disabled = false }: {
  value: ProcessingMode; onChange: (value: ProcessingMode) => void; disabled?: boolean;
}) {
  const name = useId();
  return <fieldset disabled={disabled} className="space-y-3">
    <legend className="mb-2 text-sm font-semibold text-slate-800">How should this upload be researched?</legend>
    <div className="grid gap-3 sm:grid-cols-2">{([
      ['reuse', 'Reuse saved research', 'Use compatible saved results or research when available. Research new leads normally. Faster and uses fewer AI credits.'],
      ['fresh', 'Fresh AI research', 'Research every lead again. Skip earlier qualification results and shared research caches. Takes longer and uses more AI credits.'],
    ] as const).map(([mode, title, description]) => <label key={mode} className={`flex cursor-pointer gap-3 rounded-xl border p-4 ${value === mode ? 'border-brand-400 bg-brand-50' : 'border-slate-200 bg-white'} ${disabled ? 'opacity-60' : ''}`}>
      <input className="mt-1 accent-violet-600" type="radio" name={name} value={mode} checked={value === mode} onChange={() => onChange(mode)} />
      <span><strong className="block text-sm text-slate-800">{title}</strong><span className="mt-1 block text-xs leading-relaxed text-slate-600">{description}</span></span>
    </label>)}</div>
    <p className="text-xs text-slate-500">The choice is saved when processing starts. Fresh runs keep reviewed checkboxes and manual notes; interrupted stages resume within the same run.</p>
  </fieldset>;
}
