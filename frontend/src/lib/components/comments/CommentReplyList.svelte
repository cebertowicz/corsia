<script lang="ts">
	import { resolveParticipant, userParticipant } from '$lib/stores/participants';
	import CommentMarkdownBody from './CommentMarkdownBody.svelte';
	import NewMarker from './NewMarker.svelte';
	import { onScreen, replyVisibilityThresholds } from './replyVisibility';

	export type CommentReplyData = {
		id?: string;
		author: string;
		authorKind?: 'user' | 'agent';
		authorThreadId?: string | null;
		text: string;
		at: string;
		createdAt?: string;
	};

	let { replies, newReplyIds = new Set<string>(), collapseReplyIds = newReplyIds, onNewestReplyRendered, onUnreadViewed }: {
		replies: CommentReplyData[];
		newReplyIds?: ReadonlySet<string>;
		collapseReplyIds?: ReadonlySet<string>;
		onNewestReplyRendered?: (element: HTMLElement | undefined) => void;
		onUnreadViewed?: (id: string) => Promise<void>;
	} = $props();

	const me = $derived($userParticipant);
	const who = (id: string, kind?: 'user' | 'agent') => resolveParticipant(id, me, kind);
	let expanded = $state(false);
	let newest = $state<HTMLElement>();

	const visibleFrom = $derived.by(() => {
		if (expanded || replies.length <= 3) return 0;
		const firstNew = replies.findIndex((reply) => !!reply.id && collapseReplyIds.has(reply.id));
		return firstNew >= 0 ? firstNew : replies.length - 1;
	});
	const hiddenCount = $derived(visibleFrom);

	$effect(() => {
		onNewestReplyRendered?.(newest);
		return () => onNewestReplyRendered?.(undefined);
	});

	// Chat stamps each visible unread answer, rather than the last reply in the thread.
	function unreadElement(node: HTMLElement, enabled: boolean) {
		if (!onUnreadViewed) return;
		let timer: number | undefined;
		let stamped = false;
		const cancel = () => { if (timer !== undefined) window.clearTimeout(timer); timer = undefined; };
		const observer = typeof IntersectionObserver === 'undefined' ? null : new IntersectionObserver((entries) => {
			const entry = entries.at(-1);
			if (!enabled || stamped || document.visibilityState !== 'visible' || !entry?.isIntersecting || !onScreen(entry, node)) { cancel(); return; }
			if (timer !== undefined) return;
			timer = window.setTimeout(() => {
				timer = undefined;
				if (document.visibilityState !== 'visible' || !enabled || !node.dataset.replyId) return;
				stamped = true;
				onUnreadViewed?.(node.dataset.replyId).catch((err) => { stamped = false; console.warn('Could not record the viewed reply', err); });
			}, 1_500);
		}, { root: null, threshold: replyVisibilityThresholds });
		const refresh = () => {
			cancel(); observer?.disconnect();
			if (enabled && document.visibilityState === 'visible') observer?.observe(node);
		};
		document.addEventListener('visibilitychange', refresh);
		refresh();
		return {
			update(next: boolean) { if (enabled !== next) { enabled = next; refresh(); } },
			destroy() { cancel(); observer?.disconnect(); document.removeEventListener('visibilitychange', refresh); },
		};
	}

	function newestElement(node: HTMLElement, isNewest: boolean) {
		if (isNewest) newest = node;
		return {
			update(next: boolean) {
				if (next) newest = node;
				else if (newest === node) newest = undefined;
			},
			destroy() { if (newest === node) newest = undefined; }
		};
	}
</script>

<div class="ct-replies" aria-label="Replies">
	{#if hiddenCount}
		<button class="ct-earlier" onclick={(e) => { e.stopPropagation(); expanded = true; }}>Show {hiddenCount} earlier {hiddenCount === 1 ? 'reply' : 'replies'}</button>
	{/if}
	{#each replies.slice(visibleFrom) as reply, offset (reply.id ?? `${reply.author}:${offset}`)}
		{@const index = visibleFrom + offset}
		{@const author = who(reply.author, reply.authorKind)}
		{@const isNew = !!reply.id && newReplyIds.has(reply.id)}
		<div class="ct-reply" class:new={isNew} data-reply-id={reply.id} use:newestElement={index === replies.length - 1} use:unreadElement={!!onUnreadViewed && isNew}>
			<div class="ct-msg-meta">
				<!-- The reply rail supplies grouping; an author dot would repeat it. -->
				<span class="ct-msg-author">{author.name}</span>
				<span class="ct-at">{reply.at}</span>
				{#if isNew}<NewMarker />{/if}
			</div>
			<CommentMarkdownBody text={reply.text} />
		</div>
	{/each}
</div>

<style>
	.ct-replies { display: flex; flex-direction: column; gap: var(--space-3); margin-top: var(--space-1); padding-left: var(--space-3); border-left: 1px solid var(--border); }
	.ct-earlier { align-self: flex-start; min-height: var(--space-6); padding: 0; border: 0; background: none; color: var(--text-secondary); font: inherit; font-size: var(--text-caption); font-weight: var(--fw-medium); cursor: pointer; }
	.ct-earlier:hover { text-decoration: underline; }
	.ct-reply { display: flex; flex-direction: column; gap: var(--space-1); }
	.ct-msg-meta { display: flex; align-items: center; gap: var(--space-1); }
	.ct-msg-author { font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--text-secondary); }
	.ct-at { font-size: var(--text-caption); color: var(--text-tertiary); }

</style>
