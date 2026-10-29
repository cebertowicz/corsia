<script lang="ts">
	import { fade, slide } from 'svelte/transition';
	import './comment-link.css';
	import { clipFade } from '$lib/actions/clipFade';
	import { lineDiff } from '$lib/diff';
	import ToolDiff from '$lib/components/ToolDiff.svelte';
	export type CommentThreadData = {
		id: string;
		anchor: string;
		author: string;
		authorKind?: 'user' | 'agent';
		/** The authoring agent's thread, whose presence colour its avatar shares with its caret and marks. */
		authorThreadId?: string | null;
		text: string;
		at: string;
		createdAt?: string;
		resolved: boolean;
		orphaned?: boolean;
		acked?: boolean;
		replacement?: string | null;
		insertAfter?: boolean;
		decision?: 'accepted' | 'rejected' | null;
		decidedBy?: string | null;
		replies: CommentReplyData[];
		/** Replies by others newer than this viewer's seen stamp: the server's answer, held once in the store. */
		newReplyIds?: string[];
		/** Live documents only (4.2): a row never `drawn` shows its placement on the card. */
		placement?: 'placed' | 'placing' | 'not_placed';
		drawn?: boolean;
		/** On part of an image (DCOL-FR13.4): its number there, and whether the image changed after it was pinned (FR13.9). */
		regionNumber?: number | null;
		regionStale?: boolean;
	};
	import { userParticipant, resolveParticipant } from '$lib/stores/participants';
	import Avatar from '$lib/components/ui/Avatar.svelte';
	import { agentColor } from '$lib/docPresence';
	import { retryPlacement } from '$lib/stores/docComments';
	import CommentMeta from './CommentMeta.svelte';
	import ReplyField from './ReplyField.svelte';
	import ResolvedWhileReplying from './ResolvedWhileReplying.svelte';
	import CommentMarkdownBody from './CommentMarkdownBody.svelte';
	import CommentReplyList, { type CommentReplyData } from './CommentReplyList.svelte';
	import { createReplyVisibility } from './replyVisibility';

	let { comment, focused = false, linked = false, selectable = false, focusReply = false, showReply = false, keepReplyVisible = false, flat = false, viewEachUnread = false, hideOrphan = flat, leaving = false, scrollOnFocus = true, onFocus, readOnly = false, onReply, onResolve, onAgree, onDecision, onViewed, onMovePin, tagColor = null, decisionError = '', reopenReachesOwner = false, replyText = $bindable(''), replySelection = null, onReplySelection }: {
		comment: CommentThreadData;
		focused?: boolean;
		/** Its mark is under the pointer or focus elsewhere (an image tag), so the card lights up with it. */
		linked?: boolean;
		/** An image comment in the drawer: its number is a button that selects its tag. */
		selectable?: boolean;
		/** An image tag opened this thread, so its Reply field takes focus. */
		focusReply?: boolean;
		/** A text jump exposes this thread's Reply field without moving focus from the document. */
		showReply?: boolean;
		/** Scroll this reply into its owning sheet when a short visual viewport could hide it. */
		keepReplyVisible?: boolean;
		/** Document comment surfaces present threads on the sheet rather than as nested cards. */
		flat?: boolean;
		/** Chat answers have independent seen stamps for each comment. */
		viewEachUnread?: boolean;
		/** The drawer's quote already says text changed; a flat popover still needs the state message. */
		hideOrphan?: boolean;
		/** Easing out of the list: a resolved card keeps its card form until it is gone, instead of switching to the resolved row first. */
		leaving?: boolean;
		/** Off where the caller scrolls its own list to the focused card. */
		scrollOnFocus?: boolean;
		onFocus?: () => void;
		readOnly?: boolean;
		onReply?: (commentId: string, text: string) => Promise<void>;
		onResolve?: (commentId: string, resolved: boolean) => void;
		onAgree?: (commentId: string, agreed: boolean) => void;
		/** Settles when the decision does and never rejects: a refusal comes back as `decisionError`. */
		onDecision?: (commentId: string, decision: 'accept' | 'reject') => Promise<void>;
		onViewed?: (commentId: string, replyId: string) => Promise<void>;
		/** Place an image comment's pin again, on the image as it is now. */
		onMovePin?: (commentId: string) => void;
		/** The colour of an image comment's numbered tag, which the card's number shares. */
		tagColor?: string | null;
		/** Why the last decision was refused. The caller owns it, so it outlives a card that moves groups. */
		decisionError?: string;
		/** The document has an owner thread, so a re-open reaches someone and the card may say so. */
		reopenReachesOwner?: boolean;
		/** The reply being typed. The caller may own it, so it outlives a card that moves groups. */
		replyText?: string;
		/** The reply caret/range, preserved with caller-owned draft text when a drawer remounts. */
		replySelection?: [number, number] | null;
		onReplySelection?: (selection: [number, number] | null) => void;
	} = $props();

	const me = $derived($userParticipant);
	const who = (id: string, kind?: 'user' | 'agent') => resolveParticipant(id, me, kind);
	// Avatar owns the shared initials presentation.
	const author = $derived(who(comment.author, comment.authorKind));
	const avatarColor = $derived(author.kind === 'agent' && comment.authorThreadId ? agentColor(comment.authorThreadId) : author.color);

	const isSuggestion = $derived(comment.replacement !== undefined && comment.replacement !== null);
	const suggestionDiff = $derived(isSuggestion ? lineDiff(comment.insertAfter ? '' : comment.anchor, comment.replacement!) : null);
	let replyPosting = $state(false);
	let decisionBusy = $state<'accept' | 'reject' | null>(null);
	// A placement normally completes in the same render turn as a new card. Do not flash a line that cannot be read,
	// or change the phone sheet's height during the composer-to-card swap.
	let showPlacement = $state(false);
	$effect(() => {
		if (comment.drawn || comment.placement !== 'placing') { showPlacement = false; return; }
		const timer = window.setTimeout(() => { showPlacement = true; }, 200);
		return () => window.clearTimeout(timer);
	});
	let resolvedOpen = $state(false);
	let el = $state<HTMLElement>();
	let replyField = $state<ReplyField>();
	let newestReply = $state<HTMLElement>();
	// Bumped to observe rendered replies again with fresh geometry.
	let observeTick = $state(0);
	// Plain variable: the newest reply this mounted thread has stamped, so a repeat callback cannot stamp it twice.
	let lastStamped: string | null = null;
	const stampedReplies = new Set<string>();
	// Keep the collapse point stable while this thread is mounted, even as the server clears New marks.
	// Otherwise replies would disappear from under the reader when the viewed stamp lands.
	const seenForCollapse = new Set<string>();
	const collapseReplyIds = $derived.by(() => {
		for (const id of comment.newReplyIds ?? []) seenForCollapse.add(id);
		return new Set(seenForCollapse);
	});
	// A reply becomes read only after a short uninterrupted visible dwell. Until then the server projection remains
	// the sole source for its New marker, comment pill, and attention-dock item.
	const VIEWED_DWELL_MS = 1_500;
	const dwellTimers = new Map<string, number>();
	const dwelled = new Set<string>();
	let stampTimer: number | null = null;
	const unreadKey = $derived(JSON.stringify(comment.newReplyIds ?? []));
	const replyVisibility = createReplyVisibility(dwellThenStamp, (id) => cancelDwell(id), () => cancelDwell());
	function cancelDwell(replyId?: string) {
		for (const [id, timer] of dwellTimers) {
			if (!replyId || id === replyId) { window.clearTimeout(timer); dwellTimers.delete(id); }
		}
		if (!replyId && stampTimer !== null) { window.clearTimeout(stampTimer); stampTimer = null; }
	}

	$effect(() => { if ((focusReply || (focused && selectable)) && !readOnly) replyField?.focus({ preventScroll: true }); });
	$effect(() => { if (focused && el && scrollOnFocus) el.scrollIntoView({ block: 'nearest' }); });
	// One reply's own observer calls this with that reply's id, so nothing carries over from another element.
	function stamp(replyId: string) {
		if (!onViewed || document.visibilityState !== 'visible' || !comment.newReplyIds?.length) return;
		if (viewEachUnread) {
			if (stampedReplies.has(replyId)) return;
			stampedReplies.add(replyId);
			onViewed(comment.id, replyId).catch((err) => {
				stampedReplies.delete(replyId);
				console.warn('Could not record the viewed reply', err);
			});
			return;
		}
		const position = comment.replies.findIndex((r) => r.id === replyId);
		if (position < 0 || position <= comment.replies.findIndex((r) => r.id === lastStamped)) return;
		const previous = lastStamped;
		lastStamped = replyId;
		onViewed?.(comment.id, replyId).catch((err) => {
			console.warn('Could not record the viewed reply', err);
			// A failed stamp leaves the marker; the next visibility change observes again and retries it.
			if (lastStamped === replyId) lastStamped = previous;
		});
	}
	function advanceStamp() {
		stampTimer = null;
		const unread = new Set(comment.newReplyIds ?? []);
		if (viewEachUnread) {
			for (const replyId of unread) if (dwelled.has(replyId)) stamp(replyId);
			return;
		}
		let through: string | undefined;
		for (const reply of comment.replies) {
			if (!reply.id || !unread.has(reply.id)) continue;
			if (!dwelled.has(reply.id)) break;
			through = reply.id;
		}
		if (through) stamp(through);
	}
	function dwellThenStamp(replyId: string) {
		if (document.visibilityState !== 'visible' || dwellTimers.has(replyId)) return;
		if (dwelled.has(replyId)) {
			if (stampTimer === null) stampTimer = window.setTimeout(advanceStamp, 0);
			return;
		}
		dwellTimers.set(replyId, window.setTimeout(() => {
			dwellTimers.delete(replyId);
			dwelled.add(replyId);
			// Coalesce replies that finish their dwell together into one watermark update.
			if (stampTimer === null) stampTimer = window.setTimeout(advanceStamp, 0);
		}, VIEWED_DWELL_MS));
	}
	$effect(() => {
		observeTick;
		newestReply;
		replyVisibility.sync(el, !!onViewed, JSON.parse(unreadKey));
	});
	$effect(() => () => replyVisibility.clear());
	function onVisibilityChange() {
		if (document.visibilityState === 'visible') { replyVisibility.clear(); observeTick++; }
		else cancelDwell();
	}

	async function postReply() {
		const t = replyText.trim();
		if (readOnly || !t || replyPosting) return;
		replyPosting = true;
		try {
			await onReply?.(comment.id, t);
			replyText = ''; onReplySelection?.(null);
		} finally {
			replyPosting = false;
		}
	}
	/** A press on the card focuses it; a link in the body or a press that ends a text selection keeps the press (controls stop their own). */
	function pressCard(event: MouseEvent) {
		if ((event.target as Element).closest('a') || !window.getSelection()?.isCollapsed) return;
		onFocus?.();
	}
	async function decide(decision: 'accept' | 'reject') {
		if (readOnly || decisionBusy || (decision === 'accept' && comment.orphaned)) return;
		decisionBusy = decision;
		try {
			await onDecision?.(comment.id, decision);
		} finally {
			decisionBusy = null;
		}
	}
</script>

<svelte:window onvisibilitychange={onVisibilityChange} />

{#if isSuggestion && comment.decision}
	<div class="ct resolved" class:focused bind:this={el} role="region" aria-label="{comment.decision} suggestion by {author.name}">
		<button class="ct-res-head press-tint" onclick={() => (resolvedOpen = !resolvedOpen)}>
			{#if comment.decision === 'accepted'}
				<svg class="ct-check" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
			{:else}
				<svg class="ct-cross" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12" /></svg>
			{/if}
			<span class="ct-res-text">Suggested by {author.name} · {comment.decision === 'accepted' ? 'Accepted' : 'Rejected'} by {comment.decidedBy ?? 'you'}</span>
			<svg class="ct-chev" class:open={resolvedOpen} width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6" /></svg>
		</button>
		{#if resolvedOpen}
			<div class="ct-res-body" transition:slide={{ duration: 150 }}>
				{#if suggestionDiff}<ToolDiff file="Suggested edit" lines={suggestionDiff} words />{/if}
				{#if comment.text}<CommentMarkdownBody text={comment.text} />{/if}
				{#if comment.replies.length}<CommentReplyList replies={comment.replies} newReplyIds={new Set(comment.newReplyIds ?? [])} {collapseReplyIds} onNewestReplyRendered={(element) => (newestReply = element)} />{/if}
			</div>
		{/if}
	</div>
{:else if comment.resolved && !replyText.trim() && !leaving}
	<div class="ct resolved" class:focused class:read-only={readOnly} bind:this={el} role="region" aria-label="Resolved comment by {author.name}" in:fade={{ duration: 150 }}>
		<button class="ct-res-head press-tint" onclick={() => (resolvedOpen = !resolvedOpen)}>
			<svg class="ct-check" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
			{#if comment.regionNumber != null}<span class="ct-num" style:--region-color={tagColor} aria-label="Comment {comment.regionNumber}">{comment.regionNumber}</span>{/if}
			<span class="ct-res-text">Resolved · {comment.replies.length + 1} message{comment.replies.length ? 's' : ''}</span>
			<svg class="ct-chev" class:open={resolvedOpen} width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6" /></svg>
		</button>
		{#if resolvedOpen}
			<div class="ct-res-body" transition:slide={{ duration: 150 }}>
				<CommentMarkdownBody text={comment.text} />
				{#if comment.replies.length}<CommentReplyList replies={comment.replies} newReplyIds={new Set(comment.newReplyIds ?? [])} {collapseReplyIds} onNewestReplyRendered={(element) => (newestReply = element)} />{/if}
				<div class="ct-res-acts">
					<button class="ct-link" disabled={readOnly} onclick={() => onResolve?.(comment.id, false)}>Re-open</button>
					{#if reopenReachesOwner}<span class="ct-res-hint">Re-open asks the agent to revisit.</span>{/if}
				</div>
			</div>
		{/if}
	</div>
{:else}
	<!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
	<div class="ct" class:flat class:focused class:linked class:acked={comment.acked && !comment.resolved} class:read-only={readOnly} class:orphaned={comment.orphaned} bind:this={el} role="region" aria-label="Comment by {author.name}" onclick={onFocus ? pressCard : undefined}>
		<div class="ct-head">
			{#if comment.regionNumber != null && selectable}<button type="button" class="ct-num" style:--region-color={tagColor} aria-label="Show comment {comment.regionNumber} on the image" onclick={(e) => { e.stopPropagation(); onFocus?.(); }}>{comment.regionNumber}</button>
			{:else if comment.regionNumber != null}<span class="ct-num" style:--region-color={tagColor} aria-label="Comment {comment.regionNumber}">{comment.regionNumber}</span>{/if}
			<Avatar name={author.name} color={avatarColor} hidden />
			<CommentMeta {author} at={comment.at} createdAt={comment.createdAt} />
			{#if comment.replies.length}
				<span class="ct-reply-count">{comment.replies.length} {comment.replies.length === 1 ? 'reply' : 'replies'}</span>
			{/if}
			{#if !isSuggestion && !comment.resolved}
				<button class="ct-act ack" transition:clipFade={{ axis: 'x' }} class:on={comment.acked} disabled={readOnly} onclick={(e) => { e.stopPropagation(); onAgree?.(comment.id, !comment.acked); }} aria-label="Agree" title="Agree">
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 11v9H4a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1z" /><path d="M7 11l4-7a2 2 0 0 1 3 2l-1 5h5a2 2 0 0 1 2 2.3l-1.2 6A2 2 0 0 1 16.8 21H7" /></svg>
				</button>
				<button class="ct-act resolve" transition:clipFade={{ axis: 'x' }} disabled={readOnly} onclick={(e) => { e.stopPropagation(); onResolve?.(comment.id, true); }} aria-label="Resolve" title="Resolve — close the thread">
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
				</button>
			{/if}
		</div>
		{#if showPlacement}
			<p class="ct-placement" id="comment-placement-{comment.id}" data-comment-placement>Finding its place…</p>
		{:else if !comment.drawn && comment.placement === 'not_placed'}
			<p class="ct-placement" id="comment-placement-{comment.id}" data-comment-placement>
				Couldn’t find its place
				<button class="ct-link" data-placement-retry onclick={(e) => { e.stopPropagation(); retryPlacement(comment.id); }}>Retry</button>
			</p>
		{/if}

		{#if isSuggestion}
			{#if suggestionDiff}<ToolDiff file="Suggested edit" lines={suggestionDiff} words />{/if}
			{#if comment.orphaned}<div class="ct-orphan">The text this suggestion replaces has changed.</div>{/if}
			{#if comment.text}<CommentMarkdownBody text={comment.text} />{/if}
			{#if !readOnly}
				<div class="ct-suggestion-actions">
					<button class="ct-suggestion-accept" aria-busy={decisionBusy === 'accept'} disabled={decisionBusy !== null || comment.orphaned} title={comment.orphaned ? 'The text this suggestion replaces has changed.' : undefined} onclick={(e) => { e.stopPropagation(); decide('accept'); }}>{decisionBusy === 'accept' ? 'Accepting…' : 'Accept'}</button>
					<button class="ct-link" aria-busy={decisionBusy === 'reject'} disabled={decisionBusy !== null} onclick={(e) => { e.stopPropagation(); decide('reject'); }}>{decisionBusy === 'reject' ? 'Rejecting…' : 'Reject'}</button>
				</div>
			{/if}
			{#if decisionError}<div class="ct-decision-error" role="alert">{decisionError}</div>{/if}
		{:else}
			{#if comment.resolved}<div transition:clipFade><ResolvedWhileReplying {readOnly} onReopen={() => onResolve?.(comment.id, false)} /></div>{/if}
			{#if comment.orphaned && !hideOrphan}<div class="ct-orphan">The text this comment was on has changed</div>{/if}
			{#if comment.regionStale}
				<div class="ct-orphan" data-region-stale>Image changed since this comment{#if onMovePin && !readOnly}{' '}<button class="ct-link" data-move-pin onclick={(e) => { e.stopPropagation(); onMovePin(comment.id); }}>Move pin</button>{/if}</div>
			{/if}
			<CommentMarkdownBody text={comment.text} />
		{/if}

		{#if comment.replies.length && !leaving}
			<CommentReplyList replies={comment.replies} newReplyIds={new Set(comment.newReplyIds ?? [])} {collapseReplyIds} onNewestReplyRendered={(element) => (newestReply = element)} />
		{/if}

		{#if !isSuggestion && comment.acked && !comment.resolved}
			<div class="ct-agreed"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5" /></svg> Agreed</div>
		{/if}
		{#if !readOnly}
			<ReplyField bind:this={replyField} bind:value={replyText} selection={replySelection} onSelection={onReplySelection} keepVisible={keepReplyVisible} onSend={postReply} />
		{/if}
	</div>
{/if}

<style>
	.ct {
		background: color-mix(in srgb, var(--text-primary) 4%, transparent); border: 1px solid transparent; border-radius: var(--radius-sm);
		padding: 8px 10px; display: flex; flex-direction: column; gap: 4px;
		transition: border-color var(--dur-tight), box-shadow var(--dur-tight), background-color var(--dur-tight) var(--ease-snap); cursor: pointer;
	}
	.ct.flat {
		background: none; border: 0; border-radius: 0; box-shadow: none;
		padding: 0; gap: var(--space-1);
		/* Keep a row fully revealable at either edge of the drawer's own scroller. */
		scroll-margin-block: var(--space-1);
	}
	.ct.flat.focused, .ct.flat.linked, .ct.flat.acked, .ct.flat.orphaned { background: none; border-color: transparent; box-shadow: none; }
	/* Lit with its mark on the image while either is hovered or focused; lighter than selected. */
	.ct.linked:not(.focused) { background: color-mix(in srgb, var(--text-primary) 8%, transparent); border-color: color-mix(in srgb, var(--text-primary) 14%, transparent); }
	.ct.read-only .ct-act, .ct.read-only .ct-res-acts { display: none; }
	.ct.focused { border-color: color-mix(in srgb, var(--accent) 55%, transparent); box-shadow: var(--glass-rim), 0 0 0 3px var(--accent-subtle); }
	.ct.acked { border-color: color-mix(in srgb, var(--green) 30%, transparent); }
	.ct.orphaned { border-color: color-mix(in srgb, var(--amber) 35%, transparent); }

	.ct-head { display: flex; align-items: center; gap: 7px; }
	.ct-reply-count { display: inline-flex; align-items: center; gap: var(--space-1); font-size: var(--text-caption); color: var(--text-tertiary); }
	/* Avatar owns the shared initials presentation. */
	.ct-placement { display: flex; align-items: center; gap: 8px; margin: 0; font-size: var(--text-caption); color: var(--text-tertiary); }
	.ct-act { display: flex; padding: 3px; border-radius: var(--radius-2xs); background: none; border: none; color: var(--text-tertiary); cursor: pointer; transition: opacity var(--dur-tight), background var(--dur-tight), color var(--dur-tight); }
	.ct-act.ack { margin-left: auto; }
	.ct-act.ack:hover { background: color-mix(in srgb, var(--green) 16%, transparent); color: var(--green); }
	.ct-act.ack.on { color: var(--green); }
	.ct-act.resolve:hover { background: var(--bg-hover); color: var(--text-primary); }

	.ct-orphan { font-size: var(--text-caption); color: var(--amber); }
	/* An image comment's number, in its tag's colour and shape. */
	.ct-num {
		box-sizing: border-box; flex-shrink: 0; display: grid; place-items: center; min-width: 18px; height: 18px; padding: 0 4px; border-radius: var(--radius-2xs);
		background: var(--region-color, var(--accent)); color: var(--image-region-ink); border: 1.5px solid color-mix(in srgb, var(--region-color, var(--accent)) 70%, var(--image-region-edge));
		font-size: var(--text-caption); font-weight: var(--fw-bold); font-variant-numeric: tabular-nums; line-height: 1;
	}
	button.ct-num { position: relative; font-family: inherit; cursor: pointer; }
	button.ct-num:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* A 44 px target around the 18 px number on touch. */
	@media (pointer: coarse) { button.ct-num::before { content: ''; position: absolute; inset: -13px; } }
	.ct-suggestion-actions { display: flex; align-items: center; gap: 10px; margin-top: 2px; }
	.ct-suggestion-accept { font: inherit; font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--accent-on-fill); background: var(--accent-fill); border: none; border-radius: var(--radius-xs); padding: 5px 10px; cursor: pointer; }
	.ct-suggestion-accept:disabled, .ct-link:disabled { opacity: 0.55; cursor: not-allowed; }
	.ct-suggestion-actions button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
	.ct-suggestion-accept:disabled[aria-busy="true"] { color: var(--text-primary); background: var(--accent-wash); opacity: 1; }
	.ct-link:disabled[aria-busy="true"] { color: var(--text-primary); background: var(--accent-wash); border-radius: var(--radius-xs); padding: 2px 6px; margin: -2px -6px; opacity: 1; }
	.ct-decision-error { font-size: var(--text-caption); color: var(--red-text); }

	.ct-agreed { display: flex; align-items: center; gap: 5px; font-size: var(--text-caption); font-weight: var(--fw-medium); color: var(--green); margin-top: 2px; }

	@media (pointer: coarse) { .ct-act { min-width: calc(var(--space-8) - var(--space-1)); min-height: calc(var(--space-8) - var(--space-1)); align-items: center; justify-content: center; } }

	.ct.resolved { padding: 0; background: none; cursor: default; gap: 0; }
	.ct-res-head { display: flex; align-items: center; gap: 8px; width: 100%; text-align: left; background: none; border: none; cursor: pointer; padding: 6px 10px; }
	.ct-check { color: var(--green); flex-shrink: 0; }
	.ct-cross { color: var(--text-secondary); flex-shrink: 0; }
	.ct-res-text { flex: 1; font-size: var(--text-footnote); color: var(--text-secondary); }
	.ct-chev { color: var(--text-tertiary); flex-shrink: 0; transition: transform var(--dur-tight); }
	.ct-chev.open { transform: rotate(90deg); }
	.ct-res-body { display: flex; flex-direction: column; gap: 8px; padding: 2px 10px 8px; }
	.ct-res-acts { display: flex; align-items: center; gap: 8px; }
	/* The link treatment is shared in comment-link.css. */
	.ct-res-hint { font-size: var(--text-caption); color: var(--text-tertiary); }

</style>
