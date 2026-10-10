<script lang="ts">
	/**
	 * The queue, pinned to the top of the input box.
	 *
	 * The canvas is for what has been delivered and what is about to be. An input
	 * the user parked until the turn ends is neither, so it does not live there —
	 * it waits on the composer, attached to the box it will be sent from, where a
	 * glance at the thing you are typing into also tells you what is already
	 * waiting behind it.
	 *
	 * Membership is delivery truth (`isQueuedAfterTurn`) read straight off the
	 * message store, so there is no side store to reconcile and no second copy of
	 * the queue to drift: a queued input appears here from its optimistic echo,
	 * and it leaves the instant the server says it was delivered, cancelled or
	 * refused — at which point it takes its real place in the transcript.
	 */
	import { isQueuedAfterTurn } from '$lib/messageDelivery';
	import { activeThreadId, messages } from '$lib/stores/threads';
	import QueuedInputChip from './QueuedInputChip.svelte';

	let { threadId = undefined }: { threadId?: string | null } = $props();

	// The store can hold more than one displayed thread's rows (#1949): this
	// stack shows only the queue of the thread its composer sends to.
	const boundThreadId = $derived(threadId ?? $activeThreadId);
	const queued = $derived(
		$messages.filter((msg) =>
			msg.thread_id === boundThreadId && msg.role === 'user' && isQueuedAfterTurn(msg)),
	);
</script>

{#if queued.length > 0}
	<ul
		class="queued-stack"
		aria-label="Queued messages, waiting for the current turn to finish"
	>
		{#each queued as msg, index (msg.id)}
			<!-- A place in line is worth showing only once there is a line. -->
			<QueuedInputChip {msg} position={queued.length > 1 ? index + 1 : 0} />
		{/each}
	</ul>
{/if}
