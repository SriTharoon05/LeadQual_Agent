import { useEffect, useRef, useState } from 'react';
import { isAxiosError } from 'axios';
import { useQueryClient } from '@tanstack/react-query';
import { updateLeadTracking, getLeadDetail } from '@/api/endpoints';
import { useToast } from '@/components/ui/Toast';
import type { LeadResult } from '@/types';
import { invalidateLeadData } from '@/lib/leadCache';

type Props = { lead: LeadResult; jobId: string; onSaved: (lead: LeadResult) => void; notes?: boolean };

export function LeadTracking({ lead, jobId, onSaved, notes = false }: Props) {
  const [draft, setDraft] = useState(lead.tracking_notes || '');
  const [notesDirty, setNotesDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [pendingReviewed, setPendingReviewed] = useState<boolean | null>(null);
  const [error, setError] = useState('');
  const qc = useQueryClient();
  const { toast } = useToast();
  useEffect(() => { if (!notesDirty) setDraft(lead.tracking_notes || ''); }, [lead.tracking_notes, notesDirty]);
  const save = async (changes: { reviewed?: boolean; notes?: string }) => {
    if (savingRef.current) return;
    savingRef.current = true;
    if (changes.reviewed !== undefined) setPendingReviewed(changes.reviewed);
    setSaving(true); setError('');
    try {
      const result = await updateLeadTracking(jobId, lead.lead_id, { version: lead.tracking_version, ...changes });
      onSaved(result);
      if (changes.notes !== undefined) setNotesDirty(false);
      await invalidateLeadData(qc);
      if (changes.notes !== undefined) toast('Notes saved', 'success');
    } catch (cause) {
      const conflict = isAxiosError(cause) && cause.response?.status === 409;
      const message = conflict ? 'Tracking changed elsewhere. Latest data loaded; your draft is retained. Review it before saving again.' : 'Could not save tracking. Please try again.';
      setError(message);
      toast(message, 'error');
      if (conflict) {
        try { onSaved(await getLeadDetail(jobId, lead.lead_id)); }
        catch { setError('Your draft is retained, but the latest saved details could not be loaded. Reload before saving again.'); }
        await invalidateLeadData(qc);
      }
    } finally { savingRef.current = false; setSaving(false); setPendingReviewed(null); }
  };
  return <div onClick={(event) => event.stopPropagation()} className={notes ? 'space-y-3' : ''}>
    <label className="inline-flex cursor-pointer items-center gap-2 text-sm text-slate-700">
      <input type="checkbox" checked={pendingReviewed ?? !!lead.reviewed} disabled={saving} aria-busy={saving}
        aria-label={`Reviewed: ${lead.contact_name || lead.company_name || 'lead'}`}
        onChange={(event) => void save({ reviewed: event.target.checked })}
        className="h-4 w-4 rounded border-slate-300 accent-violet-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-violet-600 disabled:opacity-50" />
      {notes && 'Reviewed'}
    </label>
    {notes && <>
      <label className="block text-xs font-medium text-slate-500" htmlFor={`notes-${lead.lead_id}`}>Tracking notes · shared across uploads</label>
      <textarea id={`notes-${lead.lead_id}`} value={draft} maxLength={10000} disabled={saving}
        onChange={(event) => { setDraft(event.target.value); setNotesDirty(true); }} rows={4}
        placeholder="Add follow-up notes, context, or next steps…"
        className="w-full rounded-lg border border-slate-200 bg-white p-3 text-sm text-slate-700 focus:border-brand-400 focus:outline-none focus:ring-2 focus:ring-brand-100" />
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-slate-400">{draft.length.toLocaleString()} / 10,000</span>
        <button type="button" disabled={saving || draft === (lead.tracking_notes || '')}
          onClick={() => void save({ notes: draft })}
          className="rounded-lg bg-brand-600 px-3 py-2 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50">{saving ? 'Saving…' : 'Save notes'}</button>
      </div>
    </>}
    {error && notes && <p role="alert" className="text-xs text-rose-700">{error}</p>}
  </div>;
}
