<script lang="ts">
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';
	import Popover from '$lib/components/ui/Popover.svelte';
	import CommentThread from '$lib/components/comments/CommentThread.svelte';
	import type { CommentThreadData } from '$lib/components/comments/CommentThreadData';

	let { surface, open, comments }: { surface: 'drawer' | 'popover'; open: boolean; comments: CommentThreadData[] } = $props();
	const anchor = new DOMRect(8, 8, 10, 10);
</script>

{#snippet threads()}
	{#each comments as comment (comment.id)}
		<CommentThread {comment} flat onReply={async () => {}} onResolve={() => {}} />
	{/each}
{/snippet}

{#if surface === 'drawer'}
	<!-- The caller keeps the drawer mounted: closing plays its outro, and a reopen during it keeps the same cards. -->
	<AdaptiveDrawer ariaLabel="Thread" {open} onClose={() => {}}>
		{@render threads()}
	</AdaptiveDrawer>
{:else}
	<Popover {anchor} {open} onClose={() => {}} restoreFocus={() => {}}>
		{@render threads()}
	</Popover>
{/if}
