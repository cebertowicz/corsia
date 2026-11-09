<script lang="ts">
	import ChatCommentSelection from './ChatCommentSelection.svelte';

	/** Test host for the pick binding, wired the way `ChatPanel` → `InputBar` → `ChatCommentSelection` is: the caller owns the picked id and whether the column is up. */
	let { onChange = () => {}, onSendReply = () => {} }: { onChange?: () => void; onSendReply?: () => void } = $props();
	let selectedCommentId = $state<string | null>(null);
	let margin = $state(true);
</script>

<div data-picked={selectedCommentId ?? ''}></div>
<button data-toggle-margin onclick={() => { margin = !margin; }}>Toggle margin</button>
<ChatCommentSelection
	threadId="thread"
	comments={[]}
	marginComments={margin}
	bind:selectedCommentId
	onChange={onChange}
	onSendReply={onSendReply}
/>
