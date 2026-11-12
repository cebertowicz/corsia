<script lang="ts">
	import ChatCommentSelection from './ChatCommentSelection.svelte';
	import type { MarginComposer } from '$lib/docMarginSource';
	import type { ChatComment } from '$lib/chatCommentAnchors';

	/** Test host for the pick binding, wired the way `ChatPanel` → `InputBar` → `ChatCommentSelection` is: the caller owns the picked id, whether the column is up, and the composer the column draws. */
	let { onChange = () => {}, onSendReply = () => Promise.resolve() }: { onChange?: () => void; onSendReply?: (reply: ChatComment) => Promise<void> } = $props();
	let selectedCommentId = $state<string | null>(null);
	let margin = $state(true);
	let marginComposer = $state<MarginComposer | null>(null);
</script>

<div data-picked={selectedCommentId ?? ''}></div>
<div data-margin-composer={marginComposer?.id ?? ''}>{#if marginComposer}{@render marginComposer.body()}{/if}</div>
<button data-toggle-margin onclick={() => { margin = !margin; }}>Toggle margin</button>
<ChatCommentSelection
	threadId="thread"
	comments={[]}
	marginComments={margin}
	bind:selectedCommentId
	bind:marginComposer
	onChange={onChange}
	onSendReply={onSendReply}
/>
