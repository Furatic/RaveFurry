/** The database is authoritative; a web restart must not create a new vote identity. */
export type PersonalVote = {occurrenceId: string, choice: number, voteRevision: number, eligible?: boolean};
const votes = new Map<string, PersonalVote>();
const pending = new Set<string>();

export function getVoteState(occurrence: string): PersonalVote | undefined {
  return votes.get(String(occurrence));
}
export function getStoredVote(occurrence: number | string): string | null {
  const vote = votes.get(String(occurrence));
  return vote ? vote.choice > 0 ? '+' : vote.choice < 0 ? '-' : '0' : null;
}
export function setStoredVote(occurrence: number | string, value: string) {
  const old = votes.get(String(occurrence));
  if (old) votes.set(String(occurrence), {...old, choice: value === '+' ? 1 : value === '-' ? -1 : 0});
  paintVotes();
}
export function setVotePending(occurrence: string, value: boolean) {
  if (value) pending.add(occurrence); else pending.delete(occurrence);
  paintVotes();
}
export function acceptVote(vote: PersonalVote, force = false) {
  if (!vote || !vote.occurrenceId || ![-1, 0, 1].includes(vote.choice)) return;
  const old = votes.get(vote.occurrenceId);
  if (!force && (pending.has(vote.occurrenceId) || (old && old.voteRevision > vote.voteRevision))) return;
  votes.set(vote.occurrenceId, vote);
  paintVotes();
}
export function paintVotes() {
  document.querySelectorAll<HTMLElement>('[data-occurrence-id]').forEach(row => {
    const occurrence = row.dataset.occurrenceId;
    const vote = votes.get(occurrence);
    const extra = row.dataset.priorityTier === 'extra';
    row.querySelectorAll<HTMLElement>('.vote-up, .vote-down').forEach(button => {
      button.classList.toggle('pressed', Boolean(vote && (button.classList.contains('vote-up') ? vote.choice > 0 : vote.choice < 0)));
      const disabled = extra || !vote || pending.has(occurrence) || button.dataset.furaticOwnVoteBlocked === 'true';
      button.dataset.voteKnown = String(Boolean(vote));
      button.setAttribute('aria-disabled', String(disabled));
      button.dataset.votePending = String(pending.has(occurrence));
    });
  });
}

document.addEventListener('furatic:personal-votes', (event: CustomEvent) => {
  const entries = Array.isArray(event.detail) ? event.detail : [];
  entries.forEach(vote => acceptVote(vote));
  // Retain only live occurrences plus in-flight mutations: no unbounded browser cache.
  const live = new Set(entries.map(v => v.occurrenceId));
  for (const id of votes.keys()) if (!live.has(id) && !pending.has(id)) votes.delete(id);
});
