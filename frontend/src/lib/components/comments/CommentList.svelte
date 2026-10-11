<script lang="ts">
	import CommentThread, { type CommentThreadData } from './CommentThread.svelte';
	type Comment = CommentThreadData & { review?: boolean };
	// A live row never drawn has no text to go to yet; its card line says why (4.2).
	const unplaced = (comment: Comment) => !comment.drawn && (comment.placement === 'placing' || comment.placement === 'not_placed');
	let { comments, documentOrder = false, focusedId = null, onFocus, onReply, onResolve, onAgree, onDecision, onViewed, decisionErrors = {}, readOnly = false, reopenReachesOwner = false }: {
		comments: Comment[];
		documentOrder?: boolean;
		focusedId?: string | null;
		onFocus?: (id: string) => void;
		onReply?: (id: string, text: string) => Promise<void>;
		onResolve?: (id: string, resolved: boolean) => void;
		onAgree?: (id: string, agreed: boolean) => void;
		onDecision?: (id: string, decision: 'accept' | 'reject') => Promise<void>;
		onViewed?: (id: string, replyId: string) => Promise<void>;
		decisionErrors?: Record<string, string>;
		readOnly?: boolean;
		reopenReachesOwner?: boolean;
	} = $props();
	// Replies being typed, kept here so a card that moves groups (its text changed, or it was resolved) keeps what was typed.
	let replyDrafts = $state<Record<string, string>>({});
	let heldOrder = $state<string[] | null>(null);
	const ordered = $derived(heldOrder ? [...comments].sort((a, b) => {
		const rank = (id: string) => { const index = heldOrder!.indexOf(id); return index < 0 ? Infinity : index; };
		return rank(a.id) - rank(b.id);
	}) : comments);
	function holdCardOrder(event: FocusEvent) {
		if (heldOrder !== null) return;
		if ((event.target as HTMLElement)?.closest('[data-comment-id]')) heldOrder = comments.map((comment) => comment.id);
	}
	function releaseCardOrder(event: FocusEvent) {
		const card = (event.target as HTMLElement)?.closest('[data-comment-id]');
		if (!(event.relatedTarget instanceof Node) || !card?.contains(event.relatedTarget)) heldOrder = null;
	}
	const reviews = $derived(comments.filter((comment) => comment.review && !comment.resolved));
	const open = $derived(comments.filter((comment) => !comment.review && !comment.resolved && !comment.orphaned));
	const orphaned = $derived(comments.filter((comment) => !comment.resolved && !!comment.orphaned));
	const resolved = $derived(comments.filter((comment) => comment.resolved));
</script>

{#if comments.length === 0}<p class="empty">No comments yet.</p>{/if}
<div class="list" role="region" aria-label="Comment threads" onfocusin={holdCardOrder} onfocusout={releaseCardOrder}>
	{#if documentOrder}
		{#each ordered as comment (comment.id)}
			<div data-comment-id={comment.id}>
				<button type="button" class="comment-jump" data-comment-jump disabled={unplaced(comment)} aria-describedby={unplaced(comment) ? `comment-placement-${comment.id}` : undefined} onclick={() => onFocus?.(comment.id)} aria-label={comment.orphaned || comment.review ? 'Show comment: text unavailable' : `Go to text: ${comment.anchor}`}>
					{comment.review ? 'Review comment' : comment.orphaned ? 'Text changed' : 'Go to text'}{comment.anchor ? `: “${comment.anchor}”` : ''}
				</button>
				<CommentThread bind:replyText={() => replyDrafts[comment.id] ?? "", (text) => replyDrafts[comment.id] = text} {comment} focused={focusedId === comment.id} {onReply} {onViewed} {onResolve} {onAgree} {onDecision} decisionError={decisionErrors[comment.id]} {readOnly} {reopenReachesOwner} />
			</div>
		{/each}
	{:else}
	{#if reviews.length}<h3>Review comments</h3>{#each reviews as comment (comment.id)}<CommentThread bind:replyText={() => replyDrafts[comment.id] ?? "", (text) => replyDrafts[comment.id] = text} {comment} focused={focusedId === comment.id} onFocus={() => onFocus?.(comment.id)} {onReply} {onViewed} {onResolve} {onAgree} {onDecision} decisionError={decisionErrors[comment.id]} {readOnly} />{/each}{/if}
	{#if open.length}<h3>Open comments</h3>{#each open as comment (comment.id)}<CommentThread bind:replyText={() => replyDrafts[comment.id] ?? "", (text) => replyDrafts[comment.id] = text} {comment} focused={focusedId === comment.id} onFocus={() => onFocus?.(comment.id)} {onReply} {onViewed} {onResolve} {onAgree} {onDecision} decisionError={decisionErrors[comment.id]} {readOnly} />{/each}{/if}
	{#if orphaned.length}<h3>Text changed</h3>{#each orphaned as comment (comment.id)}<CommentThread bind:replyText={() => replyDrafts[comment.id] ?? "", (text) => replyDrafts[comment.id] = text} {comment} focused={focusedId === comment.id} onFocus={() => onFocus?.(comment.id)} {onReply} {onViewed} {onResolve} {onAgree} {onDecision} decisionError={decisionErrors[comment.id]} {readOnly} />{/each}{/if}
	{#if resolved.length}<details><summary>Resolved · {resolved.length}</summary>{#each resolved as comment (comment.id)}<CommentThread bind:replyText={() => replyDrafts[comment.id] ?? "", (text) => replyDrafts[comment.id] = text} {comment} focused={focusedId === comment.id} onFocus={() => onFocus?.(comment.id)} {onReply} {onViewed} {onResolve} {onAgree} {onDecision} decisionError={decisionErrors[comment.id]} {readOnly} {reopenReachesOwner} />{/each}</details>{/if}
	{/if}
</div>

<style>
	.comment-jump { display: block; width: 100%; min-height: 24px; text-align: left; border: 0; background: none; color: var(--accent); font: inherit; font-size: var(--text-footnote); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
	.comment-jump:disabled { color: var(--text-tertiary); cursor: default; }
	.comment-jump:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; border-radius: var(--radius-sm); }

	.list { display: flex; flex-direction: column; gap: 4px; }
	h3, summary { margin: 6px 0 2px; color: var(--text-tertiary); font-size: var(--text-caption); font-weight: var(--fw-semibold); }
	h3:first-child { margin-top: 0; }
	.empty { margin: 0; color: var(--text-tertiary); font-size: var(--text-footnote); }
	details { display: flex; flex-direction: column; gap: 4px; }
	summary { cursor: pointer; }
</style>
