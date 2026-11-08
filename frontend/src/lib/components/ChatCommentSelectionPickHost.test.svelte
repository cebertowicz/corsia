<script lang="ts">
	import ChatCommentSelection from './ChatCommentSelection.svelte';

	/** Test host for the pick binding, wired the way `ChatPanel` → `InputBar` → `ChatCommentSelection` is: the caller owns the picked id. */
	let { onChange = () => {}, onSendReply = () => {} }: { onChange?: () => void; onSendReply?: () => void } = $props();
	let selectedCommentId = $state<string | null>(null);
</script>

<div data-picked={selectedCommentId ?? ''}></div>
<ChatCommentSelection
	threadId="thread"
	comments={[]}
	marginComments
	bind:selectedCommentId
	onChange={onChange}
	onSendReply={onSendReply}
/>
