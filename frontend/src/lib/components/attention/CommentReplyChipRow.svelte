<script lang="ts">
	import { onMount } from 'svelte';
	import { on } from '$lib/ws';
	import type { AttentionDestination, AttentionQueueItem, CommentReplyDetail } from '$lib/api/attentionQueue';
	import type { MessageCard } from '$lib/api/messageCards';
	import AttachmentRow from '$lib/components/AttachmentRow.svelte';
	import { activeThreadId } from '$lib/stores/threads';
	import { reload, visibleQueue } from '$lib/stores/attentionDock.svelte';

	let {
		onOpenDocument = () => {},
	}: {
		onOpenDocument?: (destination: AttentionDestination, threadId?: string | null) => void;
	} = $props();

	// Reply notices belong to the agent who answered. They are not Needs-you work, so
	// the dock keeps its single-job queue and this stable chip carries only unread replies.
	const replies = $derived(
		($visibleQueue.reply_items ?? $visibleQueue.items).filter((item): item is AttentionQueueItem & { detail: CommentReplyDetail } =>
			item.renderer === 'comment-reply' && (item.detail as CommentReplyDetail).source.thread_id === $activeThreadId,
		),
	);
	const unreadCount = $derived(replies.reduce((total, item) => total + (item.detail.unread_count ?? 1), 0));
	const documents = $derived.by(() => {
		const unique = new Map<string, AttentionQueueItem & { detail: CommentReplyDetail }>();
		for (const item of replies) unique.set(item.detail.destination.id, item);
		return [...unique.values()];
	});
	// Newer producers name the timestamp explicitly. Old queue rows retain their
	// existing order, which is the server's ordering contract.
	const newest = $derived.by(() => documents.reduce((latest, item) =>
		!latest || (item.detail.newest_at ?? '') > (latest.detail.newest_at ?? '') ? item : latest,
		null as (AttentionQueueItem & { detail: CommentReplyDetail }) | null,
	));
	const singleDocument = $derived(documents.length === 1 ? documents[0] : null);
	const countLabel = $derived(`${unreadCount} new ${unreadCount === 1 ? 'reply' : 'replies'}`);
	const documentLabel = $derived(singleDocument ? singleDocument.detail.title : `${documents.length} documents`);
	const chipLabel = $derived(`${countLabel} · ${documentLabel}`);
	const card = $derived.by((): MessageCard | null => {
		const target = singleDocument ?? newest;
		if (!target) return null;
		return {
			kind: 'artifact',
			artifact_id: target.detail.destination.id,
			title: chipLabel,
			type: 'document',
			icon: 'file-text',
			name: chipLabel,
			path: target.detail.destination.path ?? null,
			available: true,
			opens: true,
		};
	});

	function openReplyDocument(event: MouseEvent): void {
		// Capture keeps AttachmentRow's shared chip untouched while this source owns
		// the reply-specific landing intent that R3 consumes in the document.
		event.stopPropagation();
		const target = singleDocument ?? newest;
		if (target) onOpenDocument(target.detail.destination, target.detail.source.thread_id);
	}

	onMount(() => {
		void reload();
		const offPlans = on('plans_changed', () => { void reload(); });
		const offComments = on('doc_comment_changed', () => { void reload(); });
		return () => { offPlans(); offComments(); };
	});
</script>

{#if card && unreadCount}
	<section class="reply-chip-notice" aria-label="Unread agent replies" onclickcapture={openReplyDocument}>
		<AttachmentRow {card} threadId={$activeThreadId} variant="chip" chipWidth="content" newMarker />
	</section>
{/if}
