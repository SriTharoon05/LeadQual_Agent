import { useRef, useState } from 'react';
import { isAxiosError } from 'axios';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Pencil, Plus, Users } from 'lucide-react';
import { createTeamMember, getTeam, updateTeamMember } from '@/api/endpoints';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Input, Label } from '@/components/ui/Input';
import { useToast } from '@/components/ui/Toast';
import { invalidateLeadWorkspace } from '@/lib/workspace';
import type { TeamMember } from '@/types';

export function TeamDirectory() {
  const qc = useQueryClient();
  const { toast } = useToast();
  const team = useQuery({ queryKey: ['workspace-team'], queryFn: getTeam, refetchInterval: 60_000 });
  const [editing, setEditing] = useState<TeamMember | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [error, setError] = useState('');

  const begin = (member: TeamMember | null) => {
    setEditing(member); setName(member?.name || ''); setEmail(member?.email || ''); setError(''); setShowForm(true);
  };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (savingRef.current || !name.trim()) return;
    savingRef.current = true;
    setSaving(true); setError('');
    try {
      if (editing) await updateTeamMember(editing.id, { version: editing.version, name: name.trim(), email: email.trim() || null });
      else await createTeamMember({ name: name.trim(), email: email.trim() || null });
      setShowForm(false);
      toast(editing ? 'Team member updated' : 'Team member added', 'success');
      await qc.invalidateQueries({ queryKey: ['workspace-team'] });
      await invalidateLeadWorkspace(qc);
    } catch (cause) {
      if (isAxiosError(cause) && cause.response?.status === 409) {
        const fresh = await team.refetch();
        const member = fresh.data?.find((item) => item.id === editing?.id);
        if (member) setEditing(member);
        setError('This team member changed elsewhere or already exists. Your draft is retained; review the current directory before saving again.');
      } else {
        const detail = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
        setError(typeof detail === 'string' ? detail : 'Could not save this team member. Your draft is retained.');
      }
    } finally { savingRef.current = false; setSaving(false); }
  };
  const toggleActive = async (member: TeamMember) => {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true); setError('');
    try {
      await updateTeamMember(member.id, { version: member.version, active: !member.active });
      toast(member.active ? 'Team member deactivated; existing assignments remain visible' : 'Team member reactivated', 'success');
      await qc.invalidateQueries({ queryKey: ['workspace-team'] });
      await invalidateLeadWorkspace(qc);
    } catch (cause) {
      setError(isAxiosError(cause) && cause.response?.status === 409 ? 'This member changed elsewhere. The latest directory is loaded; try again.' : 'Could not change this member. Please try again.');
      await team.refetch();
    } finally { savingRef.current = false; setSaving(false); }
  };

  return <Card className="p-5 space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-800"><Users className="h-4 w-4 text-brand-600" />Team directory</h2>
        <p className="mt-1 text-xs text-slate-500">Add owner profiles for lead assignment. These profiles do not create login accounts.</p>
      </div>
      <Button size="sm" variant="outline" icon={<Plus className="h-3.5 w-3.5" />} disabled={saving} onClick={() => begin(null)}>Add member</Button>
    </div>
    {showForm && <form onSubmit={(event) => void submit(event)} className="rounded-xl bg-slate-50 p-4 space-y-3">
      <p className="text-xs font-semibold text-slate-600">{editing ? `Edit ${editing.name}` : 'New team member'}</p>
      <div className="grid gap-3 sm:grid-cols-2">
        <div><Label htmlFor="team-name">Name</Label><Input id="team-name" required maxLength={100} value={name} disabled={saving} onChange={(event) => setName(event.target.value)} /></div>
        <div><Label htmlFor="team-email">Email (optional)</Label><Input id="team-email" type="email" maxLength={255} value={email} disabled={saving} onChange={(event) => setEmail(event.target.value)} /></div>
      </div>
      <div className="flex gap-2"><Button size="sm" variant="secondary" type="submit" loading={saving} disabled={!name.trim()}>Save member</Button><Button size="sm" variant="ghost" type="button" disabled={saving} onClick={() => { setShowForm(false); setError(''); }}>Cancel</Button></div>
    </form>}
    {error && <p role="alert" className="text-sm text-rose-700">{error}</p>}
    {team.isPending ? <p className="text-sm text-slate-500">Loading team…</p> : team.isError ? <p role="alert" className="text-sm text-rose-700">Could not load the directory. <button className="underline" onClick={() => void team.refetch()}>Retry</button></p> : !team.data?.length ? <p className="text-sm text-slate-400">No team members yet. Add a member, then assign leads from their details.</p> :
      <div className="max-h-72 overflow-y-auto divide-y divide-slate-100">
        {team.data.map((member) => <div key={member.id} className="flex items-center justify-between gap-3 py-3">
          <div className="min-w-0"><p className="truncate text-sm font-medium text-slate-800">{member.name} {!member.active && <span className="text-xs font-normal text-slate-400">Inactive</span>}</p><p className="truncate text-xs text-slate-500">{member.email || 'No email provided'}</p></div>
          <div className="flex shrink-0 gap-1"><Button size="sm" variant="ghost" disabled={saving} icon={<Pencil className="h-3 w-3" />} onClick={() => begin(member)}>Edit</Button><Button size="sm" variant="ghost" disabled={saving} onClick={() => void toggleActive(member)}>{member.active ? 'Deactivate' : 'Reactivate'}</Button></div>
        </div>)}
      </div>}
  </Card>;
}
