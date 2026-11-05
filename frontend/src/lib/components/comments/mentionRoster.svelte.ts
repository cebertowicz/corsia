import { untrack } from 'svelte';
import { createDocumentReviewState } from '$lib/docReviewState.svelte';
import { provideMentionRoster } from './mentionContext';

export function loadMentionRoster(target: () => { artifactId: string | null; threadId: string | null }) {
	const model = createDocumentReviewState();
	// The picker reads the tag roster's own failure, never the reviewer list's.
	provideMentionRoster({
		get taggableAgents() { return model.taggableAgents; }, get loadState() { return model.loadState; },
		get rosterError() { return model.tagRosterError; },
		// Tags need the chat the document was opened from; the server refuses a tag without one.
		get noViewingThread() { return !target().threadId; }, load: () => model.load(),
	});
	$effect(() => {
		const { artifactId, threadId } = target();
		untrack(() => { if (model.setTarget(artifactId, threadId, artifactId)) void model.load(); });
	});
}
