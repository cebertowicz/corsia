<script lang="ts">
	// Stands in for ChatPanel: one ask card inside the chat column, as a message or update row renders it.
	import MessageArtifactCards from '$lib/components/MessageArtifactCards.svelte';

	const card = {
		kind: 'ask' as const, artifact_id: 'a1', title: 'Launch plan', type: 'document', icon: 'file-text',
		name: 'launch-plan.md', path: 'launch-plan.md', available: true, opens: true, hold_id: 'h1', state: 'needs_review' as const,
	};
</script>

<MessageArtifactCards cards={[card]} threadId="t1" />
