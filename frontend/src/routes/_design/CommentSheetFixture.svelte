<script lang="ts">
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';
	import CommentComposer from '$lib/components/comments/CommentComposer.svelte';
	import CommentThread, { type CommentThreadData } from '$lib/components/comments/CommentThread.svelte';

	/**
	 * The comment sheet as a client-only fixture: a new-comment composer (`kind=draft`) or one thread with its reply
	 * field (`kind=reply`), in the real drawer. Loaded through `?frame=comment-sheet` so it owns its own viewport.
	 */
	let { kind = 'draft', text = '' }: { kind?: 'draft' | 'reply'; text?: string } = $props();
	let replyText = $state('');
	$effect(() => { replyText = text; });
	const comment: CommentThreadData = {
		id: 'c1', anchor: 'Move the decision above the actions row', author: 'agent-1', authorKind: 'agent',
		text: 'Could we keep the old wording here?', at: 'now', resolved: false, replies: [],
	};
	const noop = () => {};
</script>

<div class="page-stub">Document</div>
<AdaptiveDrawer ariaLabel="Document comments" heading={kind === 'draft' ? 'New comment' : 'Comments'} followKeyboard onClose={noop}>
	{#if kind === 'draft'}
		<CommentComposer quote="Move the decision above the actions row" body={text} onSubmit={async () => {}} onCancel={noop} />
	{:else}
		<CommentThread {comment} showReply bind:replyText onReply={async () => {}} />
	{/if}
</AdaptiveDrawer>

<style>
	.page-stub { padding: var(--space-6); color: var(--text-tertiary); }
</style>
