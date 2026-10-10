/** The agent behind a server update while it is applied to the live text, if any. */
export interface LiveUpdateAuthor {
	threadId: string;
	name: string;
	/** The streamed tool call this write commits, whose preview it takes over. */
	callId?: string;
}

let current: LiveUpdateAuthor | null = null;

/** Run an update apply with its writer known; the editor transaction it produces reads it. */
export function withLiveUpdateAuthor<T>(author: LiveUpdateAuthor | null, apply: () => T): T {
	const previous = current;
	current = author;
	try {
		return apply();
	} finally {
		current = previous;
	}
}

export function currentLiveUpdateAuthor(): LiveUpdateAuthor | null {
	return current;
}

/** Validate the broadcast author field; anything malformed is treated as no author. */
export function liveUpdateAuthor(value: unknown): LiveUpdateAuthor | null {
	if (!value || typeof value !== 'object') return null;
	const { thread_id: threadId, name, call_id: callId } = value as Record<string, unknown>;
	if (typeof threadId !== 'string' || !threadId || typeof name !== 'string' || !name) return null;
	return typeof callId === 'string' && callId ? { threadId, name, callId } : { threadId, name };
}
