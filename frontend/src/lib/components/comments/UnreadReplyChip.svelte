<script lang="ts">
	import type { MessageCard } from '$lib/api/messageCards';
	import AttachmentRow from '$lib/components/AttachmentRow.svelte';

	// The document notice uses the same attachment chip and New marker as the chat notice.
	let { artifactId, path, threadId, count, where = null, onOpen }: {
		artifactId: string;
		path: string | null;
		threadId: string | null | undefined;
		count: number;
		/** What the chip adds after the count when the reader is not already in the document: its name, or "N documents". */
		where?: string | null;
		onOpen: () => void;
	} = $props();

	const label = $derived(`${count} new ${count === 1 ? 'reply' : 'replies'}${where ? ` · ${where}` : ''}`);
	const card = $derived<MessageCard>({
		kind: 'artifact',
		artifact_id: artifactId,
		title: label,
		type: 'document',
		icon: 'file-text',
		name: label,
		path,
		available: true,
		opens: true,
	});

	function open(event: MouseEvent): void {
		// Capture keeps AttachmentRow's shared chip untouched while this notice owns where a tap goes: the chip's own
		// open would only add a tab for a document the caller already reaches in its own way.
		event.stopPropagation();
		onOpen();
	}
</script>

{#if count > 0}
	<section class="reply-chip-notice" aria-label="Unread agent replies" onclickcapture={open}>
		<!-- The chip is only tappable with a thread to open into; the capture above takes the tap either way. -->
		<AttachmentRow {card} threadId={threadId ?? artifactId} variant="chip" chipWidth="content" newMarker />
	</section>
{/if}

<style>
	/* Extend the button's hit area without changing the shared chip face. */
	.reply-chip-notice :global(button.tile.chip) { position: relative; }
	.reply-chip-notice :global(button.tile.chip)::after {
		content: '';
		position: absolute;
		inset-inline: 0;
		top: 50%;
		height: var(--touch-target);
		transform: translateY(-50%);
	}
</style>
