import { saveFailureText } from '../saveFailure';
import type { Thread } from './threads';
import { loadThreads } from './threadsLoad';

export interface ThreadLocalOnly {
	effective: boolean;
	cause: 'inherited' | 'project' | 'own' | 'off';
	inheritedFromThreadId: string | null;
	inheritedFromTitle: string | null;
}

/** Read a thread's Local only state and cause from its payload fields alone. */
export function threadLocalOnly(thread: Partial<Thread> | null | undefined): ThreadLocalOnly {
	return {
		effective: thread?.local_only_effective === true,
		cause: thread?.local_only_cause ?? 'off',
		inheritedFromThreadId: thread?.local_only_inherited_from_thread_id ?? null,
		inheritedFromTitle: thread?.local_only_inherited_from_title ?? null,
	};
}

/** Write a thread's Local only through the person-only route, then reload its canonical payload. */
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
	try {
		await loadThreads(auth);
		return { ok: true };
	} catch {
		return { ok: false, error: 'Saved. Reload the page to see it.' };
	}
}
