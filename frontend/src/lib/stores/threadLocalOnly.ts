import { saveFailureText } from '../saveFailure';
import { threads, type Thread } from './threads';

export interface ThreadLocalOnly {
	effective: boolean;
	/** The thread's own switch is on and it inherited nothing. */
	own: boolean;
	inheritedFromThreadId: string | null;
	inheritedFromTitle: string | null;
}

/** Read a thread's Local only state from its payload fields, with the stored value as the fallback for `effective`. */
export function threadLocalOnly(thread: Thread | null | undefined): ThreadLocalOnly {
	const stored = thread?.local_only === true || thread?.local_only === 1;
	const inheritedFromThreadId = thread?.local_only_inherited_from_thread_id ?? null;
	return {
		effective: thread?.local_only_effective ?? (stored || inheritedFromThreadId != null),
		own: stored && inheritedFromThreadId == null,
		inheritedFromThreadId,
		inheritedFromTitle: thread?.local_only_inherited_from_title ?? null,
	};
}

/** Write a thread's Local only through the person-only route; the store changes only once the save is confirmed. */
export async function saveThreadLocalOnly(
	threadId: string,
	localOnly: boolean,
	auth: string,
): Promise<{ ok: true } | { ok: false; error: string }> {
	let response: Response;
	try {
		response = await fetch(`/api/threads/${encodeURIComponent(threadId)}/local-only`, {
			method: 'PUT',
			headers: { Authorization: `Bearer ${auth}`, 'Content-Type': 'application/json' },
			body: JSON.stringify({ local_only: localOnly }),
		});
	} catch {
		return { ok: false, error: saveFailureText(null, false) };
	}
	if (!response.ok) {
		const body = await response.json().catch(() => null);
		return { ok: false, error: saveFailureText(body?.message ?? body?.detail, true) };
	}
	threads.update((rows) => rows.map((row) => row.id === threadId
		? { ...row, local_only: localOnly, local_only_effective: localOnly }
		: row));
	return { ok: true };
}
