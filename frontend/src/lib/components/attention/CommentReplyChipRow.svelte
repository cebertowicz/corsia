<script lang="ts">
	import type { AttentionDestination, CommentReplyItem } from '$lib/api/attentionQueue';
	import type { MessageCard } from '$lib/api/messageCards';
	import AttachmentRow from '$lib/components/AttachmentRow.svelte';
	import { activeThreadId } from '$lib/stores/threads';
	import { visibleQueue } from '$lib/stores/attentionDock.svelte';
	import { chatReplyAttention } from '$lib/stores/chatReplyAttention';
	import { replyItemsForThread } from './replyItems';

	let {
		onOpenDocument = () => {},
	}: {
		onOpenDocument?: (destination: AttentionDestination, threadId?: string | null) => void;
	} = $props();

	// Reply notices belong to the agent who answered. They are not Needs-you work, so
	// the dock keeps its single-job queue and this stable chip carries only unread replies.
	const replies = $derived([
		...replyItemsForThread($visibleQueue, $activeThreadId),
		...($chatReplyAttention?.threadId === $activeThreadId ? $chatReplyAttention.items : []),
	]);
	const unreadCount = $derived(replies.reduce((total, item) => total + item.detail.unread_count, 0));
	const documents = $derived.by(() => {
		const unique = new Map<string, CommentReplyItem>();
		for (const item of replies) unique.set(item.detail.destination.id, item);
		return [...unique.values()];
	});
	const newest = $derived.by(() => documents.reduce((latest, item) =>
		!latest || item.detail.newest_at > latest.detail.newest_at ? item : latest,
		null as CommentReplyItem | null,
	));
	const singleDocument = $derived(documents.length === 1 ? documents[0] : null);
	const countLabel = $derived(`${unreadCount} new ${unreadCount === 1 ? 'reply' : 'replies'}`);
	const documentLabel = $derived(singleDocument ? singleDocument.detail.title : `${documents.length} documents`);
	const chipLabel = $derived(documents.some(item => item.detail.destination.pane === 'chat-replies') ? countLabel : `${countLabel} · ${documentLabel}`);
	const card = $derived.by((): MessageCard | null => {
		const target = singleDocument ?? newest;
		if (!target) return null;
		return {
			kind: 'artifact',
			artifact_id: target.detail.destination.id,
			title: chipLabel,
			type: target.detail.destination.pane === 'chat-replies' ? 'chat' : 'document',
			icon: target.detail.destination.pane === 'chat-replies' ? 'message-square' : 'file-text',
			name: chipLabel,
			path: target.detail.destination.path ?? null,
			available: true,
			opens: true,
		};
	});

	function openReplyDocument(): void {
		const target = singleDocument ?? newest;
		if (target) onOpenDocument(target.detail.destination, target.detail.source.thread_id);
	}

</script>

{#if card && unreadCount}
	<section class="reply-chip-notice" aria-label="Unread agent replies">
		<AttachmentRow {card} threadId={$activeThreadId} variant="chip" chipWidth="content" newMarker onActivate={openReplyDocument} />
	</section>
{/if}
