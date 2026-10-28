<script lang="ts">
	import type { MessageCard } from '$lib/api/messageCards';
	import AttachmentRow from '$lib/components/AttachmentRow.svelte';

	// One notice for unread replies in both the chat dock and document.
	let { artifactId, path, threadId, count, where = null, type = 'document', onOpen }: {
		artifactId: string;
		path: string | null;
		threadId: string | null | undefined;
		count: number;
		/** What the chip adds after the count when the reader is not already in the document: its name, or "N documents". */
		where?: string | null;
		type?: 'chat' | 'document';
		onOpen: () => void;
	} = $props();

	const label = $derived(`${count} new ${count === 1 ? 'reply' : 'replies'}${where ? ` · ${where}` : ''}`);
	const card = $derived<MessageCard>({
		kind: 'artifact',
		artifact_id: artifactId,
		title: label,
		type,
		icon: type === 'chat' ? 'message-square' : 'file-text',
		name: label,
		path,
		available: true,
		opens: true,
	});

</script>

{#if count > 0}
	<section class="reply-chip-notice" aria-label="Unread agent replies">
		<AttachmentRow {card} threadId={threadId ?? artifactId} variant="chip" chipWidth="content" newMarker onActivate={onOpen} />
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
