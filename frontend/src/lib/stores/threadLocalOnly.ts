import { saveFailureText } from '../saveFailure';
import { threads, type Thread } from './threads';

export interface ThreadLocalOnly {
	effective: boolean;
	cause: 'inherited' | 'project' | 'own' | 'off';
	inheritedFromThreadId: string | null;
	inheritedFromTitle: string | null;
}

/** Read a thread's Local only state and cause from its payload fields alone. */
export function threadLocalOnly(thread: Thread | null | undefined): ThreadLocalOnly {
	const stored = thread?.local_only === true || thread?.local_only === 1;
	const inheritedFromThreadId = thread?.local_only_inherited_from_thread_id ?? null;
	const effective = thread?.local_only_effective ?? (stored || inheritedFromThreadId != null);
	const cause = inheritedFromThreadId !== null
		? 'inherited'
		: thread?.local_only_from_project === true
			? 'project'
			: stored ? 'own' : 'off';
	return {
		effective,
		cause,
		inheritedFromThreadId,
		inheritedFromTitle: thread?.local_only_inherited_from_title ?? null,
	};
}

type LocalOnlyThreadFields = Pick<
	Thread,
	| 'id'
	| 'local_only'
	| 'local_only_effective'
	| 'local_only_from_project'
	| 'local_only_inherited_from_thread_id'
	| 'local_only_inherited_from_title'
>;

function isLocalOnlyThreadFields(value: unknown): value is LocalOnlyThreadFields {
	if (typeof value !== 'object' || value === null) return false;
	const row = value as Record<string, unknown>;
	return typeof row.id === 'string'
		&& (typeof row.local_only === 'number' || typeof row.local_only === 'boolean' || row.local_only === null)
		&& typeof row.local_only_effective === 'boolean'
		&& typeof row.local_only_from_project === 'boolean'
		&& (typeof row.local_only_inherited_from_thread_id === 'string' || row.local_only_inherited_from_thread_id === null)
		&& (typeof row.local_only_inherited_from_title === 'string' || row.local_only_inherited_from_title === null);
}

/** Write a thread's Local only through the person-only route; merge only its confirmed family payload. */
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
	const body = await response.json().catch(() => null);
	const payloadThreads: unknown[] = Array.isArray(body?.threads) ? body.threads : [];
	const updates = new Map<string, LocalOnlyThreadFields>(
		payloadThreads.filter(isLocalOnlyThreadFields).map((row) => [row.id, row]),
	);
	if (!updates.has(threadId)) return { ok: false, error: saveFailureText(null, false) };
	threads.update((rows) => rows.map((row) => {
		const update = updates.get(row.id);
		return update ? {
			...row,
			local_only: update.local_only,
			local_only_effective: update.local_only_effective,
			local_only_from_project: update.local_only_from_project,
			local_only_inherited_from_thread_id: update.local_only_inherited_from_thread_id,
			local_only_inherited_from_title: update.local_only_inherited_from_title,
		} : row;
	}));
	return { ok: true };
}
