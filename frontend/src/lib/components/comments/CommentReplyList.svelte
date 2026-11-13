<script lang="ts" module>
	/** A thread with up to this many replies shows them whole; a longer one is long, and a list sums it up. */
	export const WHOLE_REPLIES = 5;
</script>

<script lang="ts">
	import { resolveParticipant, userParticipant } from '$lib/stores/participants';
	import CommentMarkdownBody from './CommentMarkdownBody.svelte';
	import NewMarker from './NewMarker.svelte';
	import MiddleName from '$lib/components/MiddleName.svelte';

	export type CommentReplyData = {
		id?: string;
		author: string;
		authorKind?: 'user' | 'agent';
		authorThreadId?: string | null;
		text: string;
		taggedAgents?: { id: string; name: string }[];
		/** An agent asked the person here; the value is the person's own account name. */
		taggedPerson?: string | null;
		mentions?: import('./mentionSelection').MentionOccurrence[];
		at: string;
		createdAt?: string;
	};

	let { replies, newReplyIds = new Set<string>(), collapseReplyIds = newReplyIds, view = 'recent', onOpen, onNewestReplyRendered }: {
		replies: CommentReplyData[];
		newReplyIds?: ReadonlySet<string>;
		collapseReplyIds?: ReadonlySet<string>;
		/** `recent`: up to 5 whole, a longer thread from its earliest unread (or its last 2) with Show earlier; `all`: every reply; `summary`: up to 5 whole, a longer thread a row saying how many, then its unread replies, or the newest when none is. */
		view?: 'recent' | 'all' | 'summary';
		/** The summary row opens the whole thread; without it the row shows every reply in place. */
		onOpen?: () => void;
		onNewestReplyRendered?: (element: HTMLElement | undefined) => void;
	} = $props();

	const me = $derived($userParticipant);
	const who = (id: string, kind?: 'user' | 'agent') => resolveParticipant(id, me, kind);
	let expanded = $state(false);
	let newest = $state<HTMLElement>();

	const summary = $derived(view === 'summary' && !expanded && replies.length > WHOLE_REPLIES);
	const unreadCount = $derived(replies.filter((reply) => !!reply.id && newReplyIds.has(reply.id)).length);
	const visibleFrom = $derived.by(() => {
		// Unread is never hidden behind the row: the summary shows every reply from the earliest unread one on, or only the newest.
		const firstNew = replies.findIndex((reply) => !!reply.id && collapseReplyIds.has(reply.id));
		if (summary) return firstNew >= 0 ? Math.min(firstNew, replies.length - 1) : replies.length - 1;
		// Up to 5 replies show whole; a longer thread shows everything from its earliest unread reply on, or its last 2 when none is unread.
		if (expanded || view === 'all' || replies.length <= WHOLE_REPLIES) return 0;
		return firstNew >= 0 ? Math.min(firstNew, replies.length - 2) : replies.length - 2;
	});
	const hiddenCount = $derived(visibleFrom);

	$effect(() => {
		onNewestReplyRendered?.(newest);
		return () => onNewestReplyRendered?.(undefined);
	});

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
	{#if summary}
		<button class="ct-earlier ct-open-thread" onclick={(e) => { e.stopPropagation(); if (onOpen) onOpen(); else expanded = true; }}>{`${replies.length} replies${unreadCount ? ` · ${unreadCount} new` : ''}`}<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18l6-6-6-6" /></svg></button>
	{:else if hiddenCount}
		<button class="ct-earlier" onclick={(e) => { e.stopPropagation(); expanded = true; }}>Show {hiddenCount} earlier {hiddenCount === 1 ? 'reply' : 'replies'}</button>
	{/if}
	{#each replies.slice(visibleFrom) as reply, offset (reply.id ?? `${reply.author}:${offset}`)}
		{@const index = visibleFrom + offset}
		{@const author = who(reply.author, reply.authorKind)}
		{@const isNew = !!reply.id && newReplyIds.has(reply.id)}
		<div class="ct-reply" class:new={isNew} data-reply-id={reply.id} use:newestElement={index === replies.length - 1}>
			<div class="ct-msg-meta">
				<!-- The reply rail supplies grouping; an author dot would repeat it. -->
				<span class="ct-msg-author" title={author.name}><MiddleName name={author.name} /></span>
				<span class="ct-at">{reply.at}</span>
				{#if isNew}<NewMarker />{/if}
			</div>
			<CommentMarkdownBody text={reply.text} unread={isNew} taggedAgents={reply.taggedAgents} mentions={reply.mentions} taggedPerson={reply.taggedPerson} />
		</div>
	{/each}
</div>

<style>
	.ct-replies { display: flex; flex-direction: column; gap: var(--space-3); margin-top: var(--space-1); padding-left: var(--space-3); border-left: 1px solid var(--border); }
	.ct-earlier { align-self: flex-start; display: inline-flex; align-items: center; gap: var(--space-1); min-height: var(--space-6); padding: 0; border: 0; background: none; color: var(--text-secondary); font: inherit; font-size: var(--text-caption); font-weight: var(--fw-medium); cursor: pointer; }
	.ct-earlier:hover { text-decoration: underline; }
	@media (pointer: coarse) { .ct-earlier { min-height: var(--touch-target); } }
	.ct-reply { display: flex; flex-direction: column; gap: var(--space-1); }
	/* One line, as the thread's own row: the name truncates in the middle; the time and New keep their width. */
	.ct-msg-meta { display: flex; align-items: center; gap: var(--space-1); min-width: 0; }
	.ct-msg-author { display: flex; min-width: 0; font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--text-secondary); white-space: nowrap; }
	.ct-at { flex: none; font-size: var(--text-caption); color: var(--text-tertiary); white-space: nowrap; }
	.ct-msg-meta > :global(.ct-new) { flex: none; white-space: nowrap; }

</style>
