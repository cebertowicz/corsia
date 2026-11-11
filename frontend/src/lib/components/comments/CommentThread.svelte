<script lang="ts">
	import { untrack, type Snippet } from 'svelte';
	import { fade, slide } from 'svelte/transition';
	import './comment-link.css';
	import { clipFade } from '$lib/actions/clipFade';
	import { scrollCue } from '$lib/actions/scrollCue';
	import { useLayoutCrossfade } from './layoutCrossfade.svelte';
	import { lineDiff } from '$lib/diff';
	import ToolDiff from '$lib/components/ToolDiff.svelte';
	import { changedLabel } from '$lib/docImageComments';
	import type { CommentThreadData } from './CommentThreadData';
	import { userParticipant, resolveParticipant } from '$lib/stores/participants';
	import Avatar from '$lib/components/ui/Avatar.svelte';
	import { agentColor } from '$lib/docPresence';
	import { badgeFill } from '$lib/imageRegions';
	import CommentMeta from './CommentMeta.svelte';
	import CommentSuggestionActions from './CommentSuggestionActions.svelte';
	import CommentQuote from './CommentQuote.svelte';
	import { plainQuote } from '$lib/plainQuote';
	import { isLongMessageText } from '$lib/messageCollapse';
	import NewMarker from './NewMarker.svelte';
	import CommentPlacementLine from './CommentPlacementLine.svelte';
	import ReplyField from './ReplyField.svelte';
	import { createMentionSelection, type MentionOccurrence } from './mentionSelection';
	import ResolvedWhileReplying from './ResolvedWhileReplying.svelte';
	import CommentMarkdownBody from './CommentMarkdownBody.svelte';
	import CommentReplyList from './CommentReplyList.svelte';
	import { createReplyVisibility } from './replyVisibility';
	import { createThreadOpen } from './threadOpen';
	import { observeArrivedThread } from './arrivedThread.svelte';
	import { arrivedDocComments } from '$lib/stores/docComments';
	import { decidedOpen as isDecidedOpen } from '$lib/docCanvasComments';

	let { comment, header, failure, diagram = false, drawerCard = false, focused = false, linked = false, selectable = false, focusReply = false, showReply = false, hideReply = false, inMargin = false, keepReplyVisible = false, flat = false, chatSurface = false, hideOrphan = flat, detachedNote = 'This text isn’t in the document', leaving = false, scrollOnFocus = true, autoFocusSelection = true, onFocus, readOnly = false, onReply, onResolve, onAgree, onDecision, onViewed, onMovePin, tagColor = null, decisionError = '', reopenReachesOwner = false, kept = false, replyText = $bindable(''), replySelection = null, onReplySelection, selectedTags = createMentionSelection() }: {
		comment: CommentThreadData;
		header?: Snippet;
		failure?: Snippet;
		diagram?: boolean;
		/** Keep resolved threads in the drawer's card presentation. */
		drawerCard?: boolean;
		focused?: boolean;
		/** Its mark is under the pointer or focus elsewhere (an image tag), so the card lights up with it. */
		linked?: boolean;
		/** An image comment in the drawer: its number is a button that selects its tag. */
		selectable?: boolean;
		/** An image tag opened this thread, so its Reply field takes focus. */
		focusReply?: boolean;
		/** A text jump exposes this thread's Reply field without moving focus from the document. */
		showReply?: boolean;
		/** An unpicked margin card has no Reply field; one holding an unsent draft keeps it. */
		hideReply?: boolean;
		/** A card in the margin column: focusing it adds the quote, replies and Reply field, and leaves the header's one row (names stay as they read at rest). */
		inMargin?: boolean;
		/** Scroll this reply into its owning sheet when a short visual viewport could hide it. */
		keepReplyVisible?: boolean;
		/** Document comment surfaces present threads on the sheet rather than as nested cards. */
		flat?: boolean;
		/** The chat surface's mode: each answer keeps its own seen stamp, the card opens through the read dwell instead of on arrival, a typed reply is kept after posting, and Agree/Resolve show only when the caller supplies them. */
		chatSurface?: boolean;
		/** The drawer's quote already says text changed; a flat popover still needs the state message. */
		hideOrphan?: boolean;
		/** What a detached comment's anchor no longer resolves to, in the caller's words: a chat message is not a document. */
		detachedNote?: string;
		/** Easing out of the list: a resolved card keeps its card form until it is gone, instead of switching to the resolved row first. */
		leaving?: boolean;
		/** Off where the caller scrolls its own list to the focused card. */
		scrollOnFocus?: boolean;
		/** A multi-selection may mark several cards without moving keyboard focus into each one. */
		autoFocusSelection?: boolean;
		onFocus?: () => void;
		readOnly?: boolean;
		onReply?: (commentId: string, text: string, taggedAgentIds: string[], mentions: MentionOccurrence[]) => Promise<void>;
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
		/** Resolved by someone else while this person was replying (docKeptThreads): shown as the full card, not the resolved row. */
		kept?: boolean;
		/** The reply being typed. The caller may own it, so it outlives a card that moves groups. */
		selectedTags?: ReturnType<typeof createMentionSelection>;
		replyText?: string;
		/** The reply caret/range, preserved with caller-owned draft text when a drawer remounts. */
		replySelection?: [number, number] | null;
		onReplySelection?: (selection: [number, number] | null) => void;
	} = $props();

	/** The number chip's fill: the viewer disc's, so a pin's digit reads the same in the drawer. */
	const chipFill = $derived(tagColor ? badgeFill(tagColor) : null);
	const me = $derived($userParticipant);
	const who = (id: string, kind?: 'user' | 'agent') => resolveParticipant(id, me, kind);
	// Avatar owns the shared initials presentation.
	const author = $derived(who(comment.author, comment.authorKind));
	const avatarColor = $derived(author.kind === 'agent' && comment.authorThreadId ? agentColor(comment.authorThreadId) : author.color);

	const isSuggestion = $derived(comment.replacement !== undefined && comment.replacement !== null);
	// A suggestion may have no text of its own; its tags still show.
	const hasBody = $derived(!!(comment.text || comment.taggedPerson || comment.taggedAgents?.length));
	/** Accepted or rejected, but still a conversation: it has replies and nobody resolved it. The card shows the conversation, not the edit. */
	const decidedOpen = $derived(isDecidedOpen(comment));
	const suggestionDiff = $derived(isSuggestion ? lineDiff(comment.insertAfter ? '' : comment.anchor, comment.replacement!) : null);
	let replyPosting = $state(false);
	/** A deleted text's quote is read in full unless it is long; then it is clamped with the Show more control chat comments use. */
	let quoteOpen = $state(false);
	let resolvedOpen = $state(untrack(() => !chatSurface && (!!comment.newComment || !!comment.newReplyIds?.length)));
	$effect(() => { if (!chatSurface && (comment.newComment || comment.newReplyIds?.length)) resolvedOpen = true; });
	let el = $state<HTMLElement>();
	const agreedBy = $derived((comment.acknowledgedBy ?? []).map((ack) => ack.name).join(', '));
	const agreeTransition = useLayoutCrossfade(() => el, () => !!agreedBy);
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
	// Chat answers retain their visible dwell; document threads stamp when the person opens the card.
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

	$effect(() => { if ((focusReply || (focused && selectable && autoFocusSelection)) && !readOnly) replyField?.focus({ preventScroll: true }); });
	$effect(() => { if (focused && el && scrollOnFocus) el.scrollIntoView({ block: 'nearest' }); });
	// One reply's own observer calls this with that reply's id, so nothing carries over from another element.
	function stamp(replyId: string) {
		if (!onViewed || document.visibilityState !== 'visible' || (!comment.newComment && !comment.newReplyIds?.length)) return;
		if (chatSurface) {
			if (stampedReplies.has(replyId)) return;
			stampedReplies.add(replyId);
			onViewed(comment.id, replyId).catch((err) => {
				stampedReplies.delete(replyId);
				console.warn('Could not record the viewed reply', err);
			});
			return;
		}
		const replyIndex = comment.replies.findIndex((r) => r.id === replyId);
		if (replyId !== comment.id && replyIndex < 0) return;
		const position = replyId === comment.id ? 0 : replyIndex + 1;
		const lastPosition = lastStamped === comment.id ? 0 : comment.replies.findIndex((r) => r.id === lastStamped) + 1;
		if (lastStamped !== null && position <= lastPosition) return;
		const previous = lastStamped;
		lastStamped = replyId;
		onViewed?.(comment.id, replyId).catch((err) => {
			console.warn('Could not record the viewed reply', err);
			// A failed stamp leaves the marker; the next opening retries it.
			if (lastStamped === replyId) lastStamped = previous;
		});
	}
	function advanceStamp() {
		stampTimer = null;
		const unread = new Set(comment.newReplyIds ?? []);
		if (chatSurface) {
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
		replyVisibility.sync(el, !!onViewed && chatSurface, JSON.parse(unreadKey));
	});
	const { openThread, openFromFocus } = createThreadOpen(() => comment, () => chatSurface, stamp);
	const onVisibilityChange = observeArrivedThread(() => el, () => comment.id, () => !chatSurface && !!onViewed, () => $arrivedDocComments.has(comment.id), openThread, () => { observeTick++; }, () => replyVisibility.clear(), cancelDwell);

	async function postReply() {
		const submittedText = replyText;
		const t = submittedText.trim();
		if (readOnly || !t || replyPosting) return;
		replyPosting = true;
		try {
			const mentions = replyField?.mentions() ?? [];
			await onReply?.(comment.id, t, [...new Set(mentions.map(item => item.agent_id))], mentions);
			if (!chatSurface || replyText === submittedText) { replyField?.clearTags(); replyText = ''; onReplySelection?.(null); }
		} catch {
			// The caller reports the refusal; keep the reply for retry.
		} finally {
			replyPosting = false;
		}
	}
	/** A press on the card focuses it; a link in the body or a press that ends a text selection keeps the press (controls stop their own). */
	function pressCard(event: MouseEvent) {
		if ((event.target as Element).closest('a') || !window.getSelection()?.isCollapsed) return;
		onFocus?.();
	}
</script>

<svelte:window onvisibilitychange={onVisibilityChange} />

{#snippet resolvedHead(mark: 'check' | 'cross', label: string)}
	<button class="ct-res-head press-tint" onclick={() => (resolvedOpen = !resolvedOpen)}>
		<svg class="ct-{mark}" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d={mark === 'check' ? 'M20 6L9 17l-5-5' : 'M18 6L6 18M6 6l12 12'} /></svg>
		{#if comment.regionNumber != null && !isSuggestion}<span class="ct-num" style:--region-color={tagColor} style:--disc-color={chipFill} aria-label="Comment {comment.regionNumber}">{comment.regionNumber}</span>{/if}
		<span class="ct-res-text">{label}</span>
		{#if comment.newComment}<NewMarker />{/if}
		<svg class="ct-chev" class:open={resolvedOpen} width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6" /></svg>
	</button>
{/snippet}

{#if isSuggestion && comment.decision && !decidedOpen}
	<!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
	<div class="ct resolved" class:drawerCard class:focused bind:this={el} role="region" aria-label="{comment.decision} suggestion by {author.name}" onclick={openThread} onfocusin={openFromFocus}>
		{@render header?.()}
		{@render resolvedHead(comment.decision === 'accepted' ? 'check' : 'cross', `Suggested by ${author.name} · ${comment.decision === 'accepted' ? 'Accepted' : 'Rejected'} by ${comment.decidedBy ?? 'you'}`)}
		{#if resolvedOpen}
			<div class="ct-res-body" transition:slide={{ duration: 150 }}>
				{#if suggestionDiff}<ToolDiff file="Suggested edit" lines={suggestionDiff} words />{/if}
				{#if hasBody}<CommentMarkdownBody text={comment.text} taggedAgents={comment.taggedAgents} mentions={comment.mentions} taggedPerson={comment.taggedPerson} />{/if}
				{#if comment.replies.length}<CommentReplyList replies={comment.replies} newReplyIds={new Set(comment.newReplyIds ?? [])} {collapseReplyIds} onNewestReplyRendered={(element) => (newestReply = element)} />{/if}
			</div>
		{/if}
	</div>
{:else if comment.resolved && !kept && !leaving}
	<!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
	<div class="ct resolved" data-autosize-room class:drawerCard class:focused class:read-only={readOnly} bind:this={el} role="region" aria-label="Resolved comment by {author.name}" in:fade={{ duration: 150 }} onclick={openThread} onfocusin={openFromFocus}>
		{@render header?.()}
		{@render resolvedHead('check', `Resolved · ${comment.replies.length + 1} message${comment.replies.length ? 's' : ''}`)}
		{@render failure?.()}
		{#if resolvedOpen}
			<div class="ct-res-body">
				<div class="ct-original" data-discussion-original use:scrollCue role="region" aria-label="Comment text"><CommentMarkdownBody text={comment.text} taggedAgents={comment.taggedAgents} mentions={comment.mentions} taggedPerson={comment.taggedPerson} /></div>
				{#if comment.replies.length}<div class="ct-reply-region" data-discussion-replies use:scrollCue role="region" aria-label="Reply history"><CommentReplyList replies={comment.replies} newReplyIds={new Set(comment.newReplyIds ?? [])} {collapseReplyIds} onNewestReplyRendered={(element) => (newestReply = element)} /></div>{/if}
				{#if !readOnly}<ReplyField {selectedTags} bind:this={replyField} bind:value={replyText} selection={replySelection} onSelection={onReplySelection} keepVisible={keepReplyVisible} onSend={postReply} />{/if}
				<div class="ct-res-acts">
					<button class="ct-link" disabled={readOnly} onclick={() => onResolve?.(comment.id, false)}>Re-open</button>
					{#if reopenReachesOwner}<span class="ct-res-hint">Re-open asks the agent to revisit.</span>{/if}
				</div>
			</div>
		{/if}
	</div>
{:else}
	<!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
	<div class="ct" data-autosize-room class:flat class:focused class:linked class:acked={comment.acked && !comment.resolved} class:read-only={readOnly} class:orphaned={comment.orphaned} class:removed={comment.orphaned && comment.detached} bind:this={el} role="region" aria-label={decidedOpen ? `Suggestion ${comment.decision}` : `Comment by ${author.name}`} onclick={(event) => { openThread(); if (onFocus) pressCard(event); }} onfocusin={openFromFocus}>
		{@render header?.()}
		<div class="ct-head">
			{#if comment.regionNumber != null && selectable}<button type="button" class="ct-num" style:--region-color={tagColor} style:--disc-color={chipFill} aria-label="Show comment {comment.regionNumber} on the image" onclick={(e) => { e.stopPropagation(); onFocus?.(); }}>{comment.regionNumber}</button>
			{:else if comment.regionNumber != null}<span class="ct-num" style:--region-color={tagColor} style:--disc-color={chipFill} aria-label="Comment {comment.regionNumber}">{comment.regionNumber}</span>{/if}
			<Avatar name={author.name} color={avatarColor} hidden />
			<CommentMeta {author} at={comment.at} createdAt={comment.createdAt} />
			{#if comment.newComment}<NewMarker />{/if}
			{#if comment.replies.length}
				<span class="ct-reply-count">{comment.replies.length} {comment.replies.length === 1 ? 'reply' : 'replies'}</span>
			{/if}
			{#if !isSuggestion && !comment.resolved && (onAgree || !chatSurface)}
				<button class="ct-act ack" transition:clipFade={{ axis: 'x' }} class:on={comment.acked} disabled={readOnly} onclick={(e) => { e.stopPropagation(); openThread(); onAgree?.(comment.id, !comment.acked); }} aria-label="Agree" title="Agree">
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 11v9H4a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1z" /><path d="M7 11l4-7a2 2 0 0 1 3 2l-1 5h5a2 2 0 0 1 2 2.3l-1.2 6A2 2 0 0 1 16.8 21H7" /></svg>
				</button>
			{/if}
			{#if ((!isSuggestion && !comment.resolved) || decidedOpen) && (onResolve || !chatSurface)}
				<button class="ct-act resolve" transition:clipFade={{ axis: 'x' }} disabled={readOnly} onclick={(e) => { e.stopPropagation(); openThread(); onResolve?.(comment.id, true); }} aria-label="Resolve" title="Resolve — close the thread">
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
				</button>
			{/if}
		</div>
		<CommentPlacementLine {comment} />

		{#if decidedOpen}
			<div class="ct-decided"><svg class="ct-{comment.decision === 'accepted' ? 'check' : 'cross'}" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d={comment.decision === 'accepted' ? 'M20 6L9 17l-5-5' : 'M18 6L6 18M6 6l12 12'} /></svg>Suggestion {comment.decision}</div>
			{#if hasBody}<div class="ct-original" data-discussion-original use:scrollCue role="region" aria-label="Comment text"><CommentMarkdownBody text={comment.text} taggedAgents={comment.taggedAgents} mentions={comment.mentions} taggedPerson={comment.taggedPerson} /></div>{/if}
			<!-- A decided card's only action is Resolve, so its card error is a refused resolve. -->
			{#if decisionError}<div class="ct-decision-error" role="alert">{decisionError}</div>{/if}
		{:else if isSuggestion}
			{#if suggestionDiff}<ToolDiff file="Suggested edit" lines={suggestionDiff} words />{/if}
			{#if comment.textChanged}<div class="ct-orphan">The text doesn't match this suggestion.</div>{:else if comment.orphaned}<div class="ct-orphan">The text this suggestion replaces isn't in the document.</div>{/if}
			{#if hasBody}<div class="ct-original" data-discussion-original use:scrollCue role="region" aria-label="Comment text"><CommentMarkdownBody text={comment.text} taggedAgents={comment.taggedAgents} mentions={comment.mentions} taggedPerson={comment.taggedPerson} /></div>{/if}
			<CommentSuggestionActions orphaned={!!comment.orphaned} textChanged={!!comment.textChanged} {readOnly} {decisionError} onDecide={(decision) => { openThread(); return onDecision?.(comment.id, decision); }} />
		{:else}
			{#if comment.resolved}<div transition:clipFade><ResolvedWhileReplying {readOnly} onReopen={() => onResolve?.(comment.id, false)} /></div>{/if}
			{#if comment.orphaned && !hideOrphan}{#if comment.detached && comment.anchor}<CommentQuote quote={plainQuote(comment.anchor)} lines={quoteOpen || !isLongMessageText(comment.anchor) ? null : 1} />{#if isLongMessageText(comment.anchor)}<button class="ct-link" data-quote-more aria-expanded={quoteOpen} onclick={(e) => { e.stopPropagation(); quoteOpen = !quoteOpen; }}>{quoteOpen ? 'Show less' : 'Show more'}</button>{/if}{/if}<div class="ct-orphan" data-detached={comment.detached || undefined}>{comment.detached ? detachedNote : 'The text this comment was on has changed'}</div>{/if}
			{#if comment.regionStale}
				<div class="ct-orphan" data-region-stale>{changedLabel(diagram)}{#if onMovePin && !readOnly}{' '}<button class="ct-link" data-move-pin onclick={(e) => { e.stopPropagation(); onMovePin(comment.id); }}>Move pin</button>{/if}</div>
			{/if}
			<div class="ct-original" data-discussion-original use:scrollCue role="region" aria-label="Comment text"><CommentMarkdownBody text={comment.text} taggedAgents={comment.taggedAgents} mentions={comment.mentions} taggedPerson={comment.taggedPerson} /></div>
		{/if}

		{#if comment.replies.length && !leaving}
			<div class="ct-reply-region" data-discussion-replies use:scrollCue role="region" aria-label="Reply history"><CommentReplyList replies={comment.replies} newReplyIds={new Set(comment.newReplyIds ?? [])} {collapseReplyIds} onNewestReplyRendered={(element) => (newestReply = element)} /></div>
		{/if}

		{@render failure?.()}
		{#if !isSuggestion && agreedBy && !comment.resolved}
			<div class="ct-agreed" transition:agreeTransition><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5" /></svg> Agreed: {agreedBy}</div>
		{/if}
		{#if !readOnly && !(hideReply && !replyText.trim())}
			<ReplyField {selectedTags} bind:this={replyField} bind:value={replyText} selection={replySelection} onSelection={onReplySelection} keepVisible={keepReplyVisible} posting={chatSurface && replyPosting} onSend={postReply} />
		{/if}
	</div>
{/if}

<style>
	.ct {
		background: var(--ct-bg, color-mix(in srgb, var(--text-primary) 4%, transparent)); border: 1px solid var(--ct-border, transparent); border-radius: var(--ct-radius, var(--radius-sm)); box-shadow: var(--ct-shadow, none);
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
	/* Its text was deleted outright: the edge is dashed, as the card says "This text isn’t in the document". */
	.ct.removed { border-style: dashed; }

	.ct-original, .ct-reply-region { overflow-y: auto; scrollbar-width: thin; --cue-top: 0px; --cue-bottom: 0px; mask-image: linear-gradient(to bottom, transparent, black var(--cue-top), black calc(100% - var(--cue-bottom)), transparent); }
	.ct-original:global([data-above='true']), .ct-reply-region:global([data-above='true']) { --cue-top: var(--space-2); }
	.ct-original:global([data-more='true']), .ct-reply-region:global([data-more='true']) { --cue-bottom: var(--space-2); }
	.ct-original:focus-visible, .ct-reply-region:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	.ct-head { display: flex; align-items: center; gap: 7px; }
	/* One row at every width: a long name shortens in the middle (CommentMeta), so the time and actions never drop below it. */
	.ct-reply-count { flex: none; white-space: nowrap; display: inline-flex; align-items: center; gap: var(--space-1); font-size: var(--text-caption); color: var(--text-tertiary); }
	/* Avatar owns the shared initials presentation. */
	.ct-act { display: flex; padding: 3px; border-radius: var(--radius-2xs); background: none; border: none; color: var(--text-tertiary); cursor: pointer; transition: opacity var(--dur-tight), background var(--dur-tight), color var(--dur-tight); }
	.ct-act.ack { margin-left: auto; }
	.ct-act.ack:hover { background: color-mix(in srgb, var(--green) 16%, transparent); color: var(--green); }
	.ct-act.ack.on { color: var(--green); }
	.ct-act.resolve:hover { background: var(--bg-hover); color: var(--text-primary); }

	.ct-orphan { font-size: var(--text-caption); color: var(--amber); }
	.ct-decision-error { font-size: var(--text-caption); color: var(--red-text); }
	.ct-decided { display: flex; align-items: center; gap: var(--space-1); font-size: var(--text-footnote); color: var(--text-secondary); text-transform: none; }
	/* An image comment's number, in its tag's colour and shape. */
	.ct-num {
		box-sizing: border-box; flex-shrink: 0; display: grid; place-items: center; min-width: 18px; height: 18px; padding: 0 4px; border-radius: var(--radius-2xs);
		background: var(--disc-color, var(--region-color, var(--accent))); color: var(--image-region-ink); border: 1.5px solid color-mix(in srgb, var(--region-color, var(--accent)) 70%, var(--image-region-edge));
		font-size: var(--text-caption); font-weight: var(--fw-bold); font-variant-numeric: tabular-nums; line-height: 1;
	}
	button.ct-num { position: relative; font-family: inherit; cursor: pointer; }
	button.ct-num:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* A 44 px target around the 18 px number on touch. */
	@media (pointer: coarse) { button.ct-num::before { content: ''; position: absolute; inset: -13px; } }
	.ct-link:disabled { opacity: 0.55; cursor: not-allowed; }

	.ct-agreed { display: flex; align-items: center; gap: 5px; font-size: var(--text-caption); font-weight: var(--fw-medium); color: var(--green); margin-top: 2px; }

	@media (pointer: coarse) { .ct-act { min-width: calc(var(--space-8) - var(--space-1)); min-height: calc(var(--space-8) - var(--space-1)); align-items: center; justify-content: center; } }

	.ct.resolved:not(.drawerCard) { padding: 0; background: none; cursor: default; gap: 0; }
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
