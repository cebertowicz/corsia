<script lang="ts">
	import type { Snippet } from 'svelte';
	import CommentThread, { type CommentThreadData } from './CommentThread.svelte';
	import { groupRows } from '$lib/docImageComments';
	import { plainQuote } from '$lib/plainQuote';
	import { leaveRow } from '$lib/actions/leaveRow';
	type Comment = CommentThreadData & { review?: boolean };
	// A live row never drawn has no text to go to yet; its card line says why (4.2).
	const unplaced = (comment: Comment) => !comment.drawn && (comment.placement === 'placing' || comment.placement === 'not_placed');
	let { comments, documentOrder = false, focusedId = null, hoveredId = null, onHover, scrollOnFocus = true, onFocus, onReply, onResolve, onAgree, onDecision, onViewed, onMovePin, tagColor, groupOf, groupHead, cellLabel, decisionErrors = {}, readOnly = false, reopenReachesOwner = false, focusReply = false, showReply = false, keepReplyVisible = false, replyDrafts = $bindable({}), replySelections = $bindable({}), leavingIds = new Set<string>(), keptIds = new Set<string>(), reduced = false }: {
		comments: Comment[];
		documentOrder?: boolean;
		focusedId?: string | null;
		/** Image comments: the one whose tag is hovered or focused lights its card, and hovering or focusing a card reports it. */
		hoveredId?: string | null;
		onHover?: (id: string | null) => void;
		/** Off where the caller scrolls the list to the focused card itself. */
		scrollOnFocus?: boolean;
		onFocus?: (id: string) => void;
		onReply?: (id: string, text: string) => Promise<void>;
		onResolve?: (id: string, resolved: boolean) => void;
		onAgree?: (id: string, agreed: boolean) => void;
		onDecision?: (id: string, decision: 'accept' | 'reject') => Promise<void>;
		onViewed?: (id: string, replyId: string) => Promise<void>;
		/** Image comments: place a pin again, and the colour of each one's numbered tag. */
		onMovePin?: (id: string) => void;
		tagColor?: (comment: Comment) => string;
		/** In document order, comments with the same key (one image's) sit together under `groupHead`. */
		groupOf?: (comment: Comment) => string | null;
		groupHead?: Snippet<[Comment[]]>;
		/** Presentation-only cell context, derived from the current document and anchor offsets. */
		cellLabel?: (comment: Comment) => string | null;
		decisionErrors?: Record<string, string>;
		readOnly?: boolean;
		reopenReachesOwner?: boolean;
		/** An image tag opened its thread, so the reply field receives focus. */
		focusReply?: boolean;
		/** A text jump exposes the selected thread's reply field. */
		showReply?: boolean;
		/** A sheet owns the card scroller, so a shortened visual viewport keeps its reply field visible. */
		keepReplyVisible?: boolean;
		/** Replies being typed, by comment id; the caller owns them so they outlive this list (a filter switch rebuilds it). */
		replyDrafts?: Record<string, string>;
		/** Reply caret/ranges, by comment id, preserved with drafts when a drawer remounts. */
		replySelections?: Record<string, [number, number]>;
		/** Threads easing out of the list before the caller removes them (a resolved card whose reply was sent or cleared). */
		leavingIds?: ReadonlySet<string>;
		/** Threads resolved by someone else while a reply to them was being typed; each shows as its full card. */
		keptIds?: ReadonlySet<string>;
		/** Reduced motion: a leaving row fades and nothing slides. */
		reduced?: boolean;
	} = $props();
	function rememberSelection(id: string, selection: [number, number] | null) {
		if (selection) replySelections[id] = selection; else delete replySelections[id];
	}
	let heldOrder = $state<string[] | null>(null);
	const ordered = $derived(heldOrder ? [...comments].sort((a, b) => {
		const rank = (id: string) => { const index = heldOrder!.indexOf(id); return index < 0 ? Infinity : index; };
		return rank(a.id) - rank(b.id);
	}) : comments);
	const rows = $derived(groupRows(ordered, groupOf ?? (() => null)));
	function holdCardOrder(event: FocusEvent) {
		if (heldOrder !== null) return;
		if ((event.target as HTMLElement)?.closest('[data-comment-id]')) heldOrder = comments.map((comment) => comment.id);
	}
	function releaseCardOrder(event: FocusEvent) {
		const card = (event.target as HTMLElement)?.closest('[data-comment-id]');
		if (!(event.relatedTarget instanceof Node) || !card?.contains(event.relatedTarget)) heldOrder = null;
	}
	function leaveCard(event: FocusEvent) {
		const card = event.currentTarget as HTMLElement;
		if (!(event.relatedTarget instanceof Node) || !card.contains(event.relatedTarget)) onHover?.(null);
	}
	const shownQuote = (comment: Comment) => comment.orphaned || comment.review ? null : cellLabel?.(comment) ?? null;
	const reviews = $derived(comments.filter((comment) => comment.review && !comment.resolved));
	const open = $derived(comments.filter((comment) => !comment.review && !comment.resolved && !comment.orphaned));
	const orphaned = $derived(comments.filter((comment) => !comment.resolved && !!comment.orphaned));
	const resolved = $derived(comments.filter((comment) => comment.resolved));
</script>

{#if comments.length === 0}<p class="empty">No comments yet.</p>{/if}
<div class="list" role="region" aria-label="Comment threads" onfocusin={holdCardOrder} onfocusout={releaseCardOrder}>
	{#if documentOrder}
		{#each rows as row (row.key ?? row.comments[0].id)}
			{#if row.key !== null}{@render groupHead?.(row.comments)}{/if}
			{#each row.comments as comment (comment.id)}
			{@const region = comment.regionNumber != null}
			<!-- An image comment's card and its mark light up together; the card itself selects its tag, so it needs no link. -->
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div
				data-comment-id={comment.id}
				class:focused={focusedId === comment.id}
				use:leaveRow={{ leaving: leavingIds.has(comment.id), reduced }}
				onpointerenter={region && onHover ? () => onHover(comment.id) : undefined}
				onpointerleave={region && onHover ? () => onHover(null) : undefined}
				onfocusin={region && onHover ? () => onHover(comment.id) : undefined}
				onfocusout={region && onHover ? leaveCard : undefined}
			>
				{#if !region}
					<button type="button" class="comment-jump" data-comment-jump disabled={unplaced(comment)} aria-describedby={unplaced(comment) ? `comment-placement-${comment.id}` : undefined} onclick={() => onFocus?.(comment.id)} aria-label={comment.orphaned || comment.review ? 'Show comment: text unavailable' : comment.anchor ? `Go to text: ${shownQuote(comment) ?? plainQuote(comment.anchor)}` : 'Go to text'}>
						{#if comment.review || comment.orphaned}{comment.review ? 'Review comment' : 'Text changed'}{comment.anchor ? `: “${plainQuote(comment.anchor)}”` : ''}{:else}{shownQuote(comment) ?? (comment.anchor ? `“${plainQuote(comment.anchor)}”` : 'Go to text')}{/if}
					</button>
				{/if}
				<CommentThread kept={keptIds.has(comment.id)} bind:replyText={() => replyDrafts[comment.id] ?? "", (text) => replyDrafts[comment.id] = text} replySelection={replySelections[comment.id] ?? null} onReplySelection={(selection) => rememberSelection(comment.id, selection)} {comment} focused={focusedId === comment.id} linked={region && hoveredId === comment.id} selectable={region} leaving={leavingIds.has(comment.id)} {scrollOnFocus} onFocus={region || !unplaced(comment) ? () => onFocus?.(comment.id) : undefined} focusReply={focusReply && focusedId === comment.id} showReply={showReply && focusedId === comment.id} keepReplyVisible={keepReplyVisible && focusedId === comment.id} {onReply} {onViewed} {onResolve} {onAgree} {onDecision} decisionError={decisionErrors[comment.id]} tagColor={tagColor?.(comment) ?? null} {onMovePin} {readOnly} {reopenReachesOwner} flat hideOrphan={documentOrder} />
			</div>
			{/each}
		{/each}
	{:else}
	{#if reviews.length}<h3>Review comments</h3>{#each reviews as comment (comment.id)}<CommentThread kept={keptIds.has(comment.id)} bind:replyText={() => replyDrafts[comment.id] ?? "", (text) => replyDrafts[comment.id] = text} replySelection={replySelections[comment.id] ?? null} onReplySelection={(selection) => rememberSelection(comment.id, selection)} {comment} focused={focusedId === comment.id} onFocus={() => onFocus?.(comment.id)} focusReply={focusReply && focusedId === comment.id} showReply={showReply && focusedId === comment.id} {onReply} {onViewed} {onResolve} {onAgree} {onDecision} decisionError={decisionErrors[comment.id]} tagColor={tagColor?.(comment) ?? null} {onMovePin} {readOnly} flat hideOrphan={documentOrder} />{/each}{/if}
	{#if open.length}<h3>Open comments</h3>{#each open as comment (comment.id)}<CommentThread kept={keptIds.has(comment.id)} bind:replyText={() => replyDrafts[comment.id] ?? "", (text) => replyDrafts[comment.id] = text} replySelection={replySelections[comment.id] ?? null} onReplySelection={(selection) => rememberSelection(comment.id, selection)} {comment} focused={focusedId === comment.id} onFocus={() => onFocus?.(comment.id)} focusReply={focusReply && focusedId === comment.id} showReply={showReply && focusedId === comment.id} {onReply} {onViewed} {onResolve} {onAgree} {onDecision} decisionError={decisionErrors[comment.id]} tagColor={tagColor?.(comment) ?? null} {onMovePin} {readOnly} flat hideOrphan={documentOrder} />{/each}{/if}
	{#if orphaned.length}<h3>Text changed</h3>{#each orphaned as comment (comment.id)}<CommentThread kept={keptIds.has(comment.id)} bind:replyText={() => replyDrafts[comment.id] ?? "", (text) => replyDrafts[comment.id] = text} replySelection={replySelections[comment.id] ?? null} onReplySelection={(selection) => rememberSelection(comment.id, selection)} {comment} focused={focusedId === comment.id} onFocus={() => onFocus?.(comment.id)} focusReply={focusReply && focusedId === comment.id} showReply={showReply && focusedId === comment.id} {onReply} {onViewed} {onResolve} {onAgree} {onDecision} decisionError={decisionErrors[comment.id]} tagColor={tagColor?.(comment) ?? null} {onMovePin} {readOnly} flat hideOrphan={documentOrder} />{/each}{/if}
	{#if resolved.length}<details><summary>Resolved · {resolved.length}</summary>{#each resolved as comment (comment.id)}<CommentThread kept={keptIds.has(comment.id)} bind:replyText={() => replyDrafts[comment.id] ?? "", (text) => replyDrafts[comment.id] = text} replySelection={replySelections[comment.id] ?? null} onReplySelection={(selection) => rememberSelection(comment.id, selection)} {comment} focused={focusedId === comment.id} onFocus={() => onFocus?.(comment.id)} focusReply={focusReply && focusedId === comment.id} showReply={showReply && focusedId === comment.id} {onReply} {onViewed} {onResolve} {onAgree} {onDecision} decisionError={decisionErrors[comment.id]} tagColor={tagColor?.(comment) ?? null} {onMovePin} {readOnly} {reopenReachesOwner} flat hideOrphan={documentOrder} />{/each}</details>{/if}
	{/if}
</div>

<style>
	.comment-jump { display: block; width: 100%; min-height: var(--space-6); padding: 0 0 0 var(--space-2); text-align: left; border: 0; border-left: 2px solid var(--border); background: none; color: var(--text-tertiary); font: inherit; font-size: var(--text-footnote); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
	:global([data-comment-id].focused) .comment-jump:not(:disabled) { border-left-color: var(--accent); }
	@media (pointer: coarse) { .comment-jump { min-height: calc(var(--space-8) - var(--space-1)); } }
	.comment-jump:disabled { color: var(--text-tertiary); cursor: default; }
	.comment-jump:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; border-radius: var(--radius-sm); }

	.list { display: flex; flex-direction: column; gap: var(--space-3); }
	.list > :global([data-comment-id]) { min-width: 0; }
	h3, summary { margin: 6px 0 2px; color: var(--text-tertiary); font-size: var(--text-caption); font-weight: var(--fw-semibold); }
	h3:first-child { margin-top: 0; }
	.empty { margin: 0; color: var(--text-tertiary); font-size: var(--text-footnote); }
	details { display: flex; flex-direction: column; gap: 4px; }
	summary { cursor: pointer; }
</style>
