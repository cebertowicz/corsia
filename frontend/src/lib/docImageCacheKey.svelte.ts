import { on } from '$lib/ws/events';

/**
 * The cache key a document view puts on its image URLs: new when the view opens, and new again when another
 * artifact in the view's thread changes (a linked image an agent replaced), so an open document shows the
 * replacement without being reopened. Call during component setup; it listens until the component is destroyed.
 */
export function createDocImageCacheKey(threadId: () => string | null | undefined, artifactId: () => string | null) {
	let key = $state(crypto.randomUUID());
	$effect(() => {
		const thread = threadId();
		const own = artifactId();
		if (!thread || !own) return;
		return on('artifacts_changed', (payload: { artifact_id?: string; affected_thread_ids?: string[] }) => {
			if (payload.artifact_id === own || !payload.affected_thread_ids?.includes(thread)) return;
			key = crypto.randomUUID();
		});
	});
	return { get current() { return key; } };
}
