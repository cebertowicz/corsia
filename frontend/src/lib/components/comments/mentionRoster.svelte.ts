import { untrack } from 'svelte';
import { createDocumentReviewState } from '$lib/docReviewState.svelte';
import { provideMentionRoster } from './mentionContext';

export function loadMentionRoster(target: () => { artifactId: string | null; threadId: string | null }) {
	const model = createDocumentReviewState();
	provideMentionRoster(model);
	$effect(() => {
		const { artifactId, threadId } = target();
		untrack(() => { if (model.setTarget(artifactId, threadId, artifactId)) void model.load(); });
	});
}
