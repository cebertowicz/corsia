<script lang="ts">
	import type { AttentionDestination, CommentReplyItem } from '$lib/api/attentionQueue';
	import UnreadReplyChip from '$lib/components/comments/UnreadReplyChip.svelte';
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
	const target = $derived(singleDocument ?? newest);
	const where = $derived(documents.some(item => item.detail.destination.pane === 'chat-replies')
		? null : singleDocument ? singleDocument.detail.title : `${documents.length} documents`);

	function openReplyDocument(): void {
		const target = singleDocument ?? newest;
		if (target) onOpenDocument(target.detail.destination, target.detail.source.thread_id);
	}

</script>

{#if target && unreadCount}
	<UnreadReplyChip artifactId={target.detail.destination.id} path={target.detail.destination.path ?? null} threadId={$activeThreadId} count={unreadCount} {where} type={target.detail.destination.pane === 'chat-replies' ? 'chat' : 'document'} onOpen={openReplyDocument} />
{/if}
