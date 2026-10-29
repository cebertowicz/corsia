import { derived, get } from 'svelte/store';
import { allThreads, threads } from './threads';
import { token } from './auth';

export const pinnedConversations = derived(threads, ($s) => {
	const set = new Set<string>();
	for (const thread of $s) {
		if (thread.pinned) set.add(thread.id);
	}
	return set;
});

export async function togglePin(id: string): Promise<void> {
	const t = get(token);
	if (!t) return;
	const current = get(allThreads).find((s) => s.id === id);
	if (!current) return;
	const next = current.pinned ? 0 : 1;

	threads.update((rows) =>
		rows.map((row) => (row.id === id ? { ...row, pinned: next } : row))
	);

	try {
		const res = await fetch(`/api/threads/${id}?token=${t}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ pinned: !!next }),
		});
		if (!res.ok) throw new Error(`pin update failed: ${res.status}`);
	} catch (err) {
		console.error('[togglePin] revert', err);
		threads.update((rows) =>
			rows.map((row) => (row.id === id ? { ...row, pinned: current.pinned ?? 0 } : row))
		);
	}
}

export function isPinned(pinned: Set<string>, id: string): boolean {
	return pinned.has(id);
}
