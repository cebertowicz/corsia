<script lang="ts">
	import ChatCommentSelection from './ChatCommentSelection.svelte';
	import ChatCommentThreads from './ChatCommentThreads.svelte';
	import { messages } from '$lib/stores/threads';

	/** Test host for reading a chat thread end to end: the transcript's highlight and the thread's margin card, both drawn from the messages store, with the card mounted the way `ChatMarginColumn` mounts it. */
	let selectedCommentId = $state<string | null>(null);
	const entries = $derived($messages.filter(message => message.thread_id === 'thread').flatMap(message => (message.chat_comments ?? []).map(comment => ({ message, comment }))));
</script>

<div data-picked={selectedCommentId ?? ''}></div>
<div class="chat-margin">
	{#each entries as entry (entry.comment.id)}
		<div class="chat-margin-card" data-card={entry.comment.id}>
			<ChatCommentThreads messageId={entry.message.id} threadId="thread" comments={[entry.comment]} margin picked={selectedCommentId === entry.comment.id} onReply={async () => {}} />
		</div>
	{/each}
</div>
<ChatCommentSelection threadId="thread" marginComments={true} bind:selectedCommentId />
