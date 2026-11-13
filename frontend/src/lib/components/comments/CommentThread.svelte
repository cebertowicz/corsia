<script lang="ts">
	import { untrack, type Snippet } from 'svelte';
	import { fade, slide } from 'svelte/transition';
	import './comment-link.css';
	import { clipFade } from '$lib/actions/clipFade';
	import type { PatchFailure } from '$lib/api/docs';
	import { scroller } from '$lib/actions/keepInView';
	import { scrollCue } from '$lib/actions/scrollCue';
	import { useLayoutCrossfade } from './layoutCrossfade.svelte';
	import { lineDiff } from '$lib/diff';
	import ToolDiff from '$lib/components/ToolDiff.svelte';
	import { changedLabel } from '$lib/docImageComments';
	import type { CommentThreadData } from './CommentThreadData';
	import { userParticipant, resolveParticipant } from '$lib/stores/participants';
	import Avatar from '$lib/components/ui/Avatar.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';
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

	let { comment, header, failure, diagram = false, drawerCard = false, focused = false, linked = false, selectable = false, focusReply = false, showReply = false, hideReply = false, inMargin = false, keepReplyVisible = false, flat = false, chatSurface = false, hideOrphan = flat, detachedNote = 'This text isn’t in the document', leaving = false, scrollOnFocus = true, autoFocusSelection = true, onFocus, readOnly = false, onReply, onResolve, onAgree, onMute, onDecision, onViewed, onMovePin, tagColor = null, decisionError = '', reopenReachesOwner = false, kept = false, replyText = $bindable(''), replySelection = null, onReplySelection, selectedTags = createMentionSelection() }: {
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
		/** Mute or unmute replies for the person, on this thread. */
		onMute?: (commentId: string, muted: boolean) => Promise<boolean | PatchFailure> | boolean | PatchFailure | void;
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
	// The mute control sits apart from Agree and Resolve, under the author's name. It answers in place: the bell turns into the app's spinner while the server decides, and a refusal takes the place of the time and replies for a moment, so nothing moves.
	let mutePhase = $state<'idle' | 'pending' | 'failed'>('idle');
	let muteWanted = $state(false);
	let muteFailure = $state<PatchFailure | null>(null);
	/** Said to a screen reader once a mute or unmute lands; empty at any other time. */
	let muteAnnounce = $state('');
	const muted = $derived(!!comment.leftByYou);
	const muteChat = $derived(comment.anchor.length > 40 ? `${comment.anchor.slice(0, 39).trimEnd()}…` : comment.anchor);
	/** Shown only where a reply has a rule to mute (not on chat comments or suggestions) and the person can reply. */
	const canMute = $derived(!!comment.replyGoesTo && !readOnly && !isSuggestion);
	/** A refused mute is said in the head's second line, after the time and the replies. */
	const muteRefused = $derived(mutePhase === 'failed' && canMute && !decidedOpen);
	// The object is the comment, as the toolbar and the sheet title say it: elsewhere in the app "chat" is the agent conversation.
	const muteTitle = $derived(mutePhase === 'pending' ? (muteWanted ? 'Muting this comment…' : 'Unmuting this comment…') : muted ? 'Unmute this comment' : 'Mute this comment');
	// Named for its comment, as a sentence a person would say, in every state including while it is pending.
	const muteName = $derived(muteChat ? `${muteTitle.replace('…', '')} on “${muteChat}”` : muteTitle);
	async function pressMute(event: MouseEvent) {
		event.stopPropagation();
		if (mutePhase === 'pending') return;
		muteWanted = !muted;
		mutePhase = 'pending';
		muteAnnounce = '';
		const result = await onMute?.(comment.id, muteWanted);
		// A refusal is `false` or its cause: the connection dropped, or CORSIA failed and minted a reference.
		const why = typeof result === 'object' && result !== null ? result : null;
		muteFailure = why;
		mutePhase = result === false || why ? 'failed' : 'idle';
		if (mutePhase === 'idle') muteAnnounce = muteWanted ? 'Comment muted' : 'Comment unmuted';
	}
	// The head stays on top while a long card scrolls under it, so Mute, Agree and Resolve are always in reach. `stuck` marks it pinned: it then gets an opaque ground.
	let head = $state<HTMLElement>();
	let stuck = $state(false);
	$effect(() => {
		const card = el, top = head;
		if (!card || !top) return;
		// Scroll events do not bubble, so one capture listener hears whichever ancestor scrolls (the card itself, the sheet's body, the document's scroller), and the owners are read at each scroll, since a margin card can be moved under another scroller after it mounts.
		const check = () => {
			const owners = [card, scroller(card)].filter((owner): owner is HTMLElement => !!owner);
			stuck = owners.some((owner) => owner.scrollTop > 0 && top.getBoundingClientRect().top <= owner.getBoundingClientRect().top + 1.5 && /auto|scroll/.test(getComputedStyle(owner).overflowY));
		};
		document.addEventListener('scroll', check, { capture: true, passive: true });
		check();
		return () => document.removeEventListener('scroll', check, { capture: true });
	});
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
	// An open margin card taller than the room beside the text keeps to that room and scrolls inside itself, so reaching its reply field moves neither the page nor the highlighted text.
	$effect(() => {
		const card = el;
		const owner = card && inMargin && focused ? scroller(card) : null;
		if (!card || !owner) return;
		const fit = () => {
			card.style.maxHeight = ''; card.style.overflowY = '';
			const room = owner.clientHeight - 2 * 8;
			if (room > 0 && card.scrollHeight > room) { card.style.maxHeight = `${room}px`; card.style.overflowY = 'auto'; }
		};
		fit();
		const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(fit) : null;
		observer?.observe(owner); observer?.observe(card);
		return () => { observer?.disconnect(); card.style.maxHeight = ''; card.style.overflowY = ''; };
	});
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

{#snippet muteButton()}
	<button type="button" class="ct-act mute" class:on={muted} class:pending={mutePhase === 'pending'} aria-busy={mutePhase === 'pending'} aria-disabled={mutePhase === 'pending' ? 'true' : undefined} aria-label={muteName} title={muteTitle} onclick={pressMute}>
		{#if mutePhase === 'pending'}<Spinner size={14} />{:else}<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.7 21a2 2 0 0 1-3.4 0" />{#if muted}<path d="M3 3l18 18" />{/if}</svg>{/if}
		<span class="ct-mute-word">{muted ? 'Muted' : 'Mute'}</span>
	</button>
	<span class="ct-sr" role="status" aria-live="polite">{muteAnnounce}</span>
{/snippet}
{#snippet muteFailed()}
	<!-- What failed and what to do (COPY-STANDARD C9, C11): the connection dropping is named; any other failure is the generic sentence, and a failure CORSIA logged carries its reference (C10), on its own line so it is never cut. -->
	<span class="error-text ct-mute-failed" class:ref={!!muteFailure?.reference} role="alert">Couldn’t {muteWanted ? 'mute' : 'unmute'}. {muteFailure?.cause === 'connection' ? 'The connection to CORSIA dropped. ' : ''}Try again.{#if muteFailure?.reference}{' '}<span class="ct-mute-ref">Reference: {muteFailure.reference}</span>{/if}</span>
{/snippet}

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
				{#if !readOnly}<ReplyField {selectedTags} bind:this={replyField} bind:value={replyText} selection={replySelection} onSelection={onReplySelection} keepVisible={keepReplyVisible} wholeCard={el} onSend={postReply} goesTo={comment.replyGoesTo?.map(agent => agent.name)} />{/if}
				<div class="ct-res-acts">
					<button class="ct-link" disabled={readOnly} onclick={() => onResolve?.(comment.id, false)}>Re-open</button>
					{#if mutePhase === 'failed' && canMute}{@render muteFailed()}{:else if reopenReachesOwner}<span class="ct-res-hint">Re-open asks the agent to revisit.</span>{/if}
					{#if canMute}{@render muteButton()}{/if}
				</div>
			</div>
		{/if}
	</div>
{:else}
	<!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
	<div class="ct" data-autosize-room class:flat class:inmargin={inMargin} class:focused class:linked class:acked={comment.acked && !comment.resolved} class:read-only={readOnly} class:orphaned={comment.orphaned} class:removed={comment.orphaned && comment.detached} bind:this={el} role="region" aria-label={decidedOpen ? `Suggestion ${comment.decision}` : `Comment by ${author.name}`} onclick={(event) => { openThread(); if (onFocus) pressCard(event); }} onfocusin={openFromFocus}>
		{@render header?.()}
		<div class="ct-head" class:stuck bind:this={head}>
			<div class="ct-who">
			{#if comment.regionNumber != null && selectable}<button type="button" class="ct-num" style:--region-color={tagColor} style:--disc-color={chipFill} aria-label="Show comment {comment.regionNumber} on the image" onclick={(e) => { e.stopPropagation(); onFocus?.(); }}>{comment.regionNumber}</button>
			{:else if comment.regionNumber != null}<span class="ct-num" style:--region-color={tagColor} style:--disc-color={chipFill} aria-label="Comment {comment.regionNumber}">{comment.regionNumber}</span>{/if}
			<Avatar name={author.name} color={avatarColor} hidden />
			<CommentMeta {author} at={comment.at} createdAt={comment.createdAt} part="author" />
			</div>
			<!-- The name and Agree and Resolve share the first line, and come first in the page's order too, so Tab walks them as the eye does. The second line is Mute, the time and the replies; a refusal takes the place of the time, the marker and the replies, so the head never changes height. -->
			<div class="ct-acts">
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
			<div class="ct-sub">
				{#if canMute && !decidedOpen}{@render muteButton()}{/if}
				{#if muteRefused}
					{@render muteFailed()}
				{:else}
					<CommentMeta {author} at={comment.at} createdAt={comment.createdAt} part="time" />
					{#if comment.newComment}<NewMarker />{/if}
					{#if comment.replies.length}
						<span class="ct-reply-count">{comment.replies.length} {comment.replies.length === 1 ? 'reply' : 'replies'}</span>
					{/if}
				{/if}
			</div>
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
			<ReplyField {selectedTags} bind:this={replyField} bind:value={replyText} selection={replySelection} onSelection={onReplySelection} keepVisible={keepReplyVisible} wholeCard={el} posting={chatSurface && replyPosting} onSend={postReply} goesTo={comment.replyGoesTo?.map(agent => agent.name)} />
		{/if}
	</div>
{/if}

<style>
	.ct {
		background: var(--ct-bg, color-mix(in srgb, var(--text-primary) 4%, transparent)); border: 1px solid var(--ct-border, transparent); border-radius: var(--ct-radius, var(--radius-sm)); box-shadow: var(--ct-shadow, none);
		padding: 8px 10px; display: flex; flex-direction: column; gap: 4px;
		transition: border-color var(--dur-tight), box-shadow var(--dur-tight), background-color var(--dur-tight) var(--ease-snap); cursor: pointer;
		/* Its 3px focus ring must stay revealable at either edge of a drawer's own scroller, bordered or flat. */
		scroll-margin-block: var(--space-1);
	}
	/* A boxed card in a sheet leaves room at its left, and the last one under it, for its own 3px halo, which the sheet's scroller would otherwise cut square. */
	:global([data-adaptive-drawer]) .ct:not(.flat) { margin-inline-start: var(--space-px-3); }
	:global([data-adaptive-drawer]) .ct:not(.flat):first-child { margin-top: var(--space-px-3); }
	:global([data-adaptive-drawer]) .ct:not(.flat):last-child { margin-bottom: var(--space-px-3); }
	.ct.flat {
		background: none; border: 0; border-radius: 0; box-shadow: none;
		padding: 0; gap: var(--space-1);
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
	/* Two lines: the name with Agree and Resolve at its right, then Mute, the time and the replies. The head stays at the top of a card or sheet that scrolls. */
	.ct-head { position: sticky; top: 0; z-index: 2; display: grid; grid-template-columns: minmax(0, 1fr) auto; column-gap: var(--space-px-7); row-gap: var(--space-1); align-items: center; }
	/* A card pads its content, and a pinned head stops at the padding's inner edge; it reaches back over that padding, so the text it scrolls past never shows beside or above it. */
	.ct:not(.flat) > .ct-head { margin: calc(-1 * var(--space-2)) calc(-1 * var(--space-px-10)) 0; padding: var(--space-2) var(--space-px-10) 0; }
	/* A margin card is its own scroller, and its pinned head stops at the padding's inner edge, so it reaches back over that padding. */
	.ct.inmargin > .ct-head { top: calc(-1 * var(--space-2)); }
	/* Pinned, the head is opaque (the text it scrolls past must not show through) and has the card's own ground, so nothing changes when it pins: the card's tint over the ground behind it. */
	.ct-head.stuck { background: var(--ct-pin, color-mix(in srgb, var(--text-primary) 4%, var(--bg-secondary))); }
	/* A flat card has no ground of its own: in a sheet the head pins on the sheet's ground and runs the full width of the sheet's body, so the reply text it scrolls past shows neither under it nor beside it. */
	.ct.flat > .ct-head.stuck { --ct-stuck-seam: 0 -1px 0 var(--ct-flat-ground, var(--bg-secondary)); background: var(--ct-flat-ground, var(--bg-secondary)); box-shadow: var(--ct-stuck-seam); }
	:global(.drawer-body) .ct.flat > .ct-head { margin-inline: calc(-1 * var(--space-4)); padding-inline: var(--space-4); }
	/* A popover pads its content by 12px and has its own ground: the head reaches back over that padding on three sides and pins at the popover's edge, on the popover's ground. */
	:global(.popover) .ct.flat { --ct-flat-ground: var(--bg-elevated); }
	:global(.popover) .ct.flat > .ct-head { top: calc(-1 * var(--space-3)); margin: calc(-1 * var(--space-3)) calc(-1 * var(--space-3)) 0; padding: var(--space-3) var(--space-3) 0; }
	.ct-who { grid-column: 1; grid-row: 1; display: flex; align-items: center; gap: var(--space-px-7); min-width: 0; }
	/* The time starts where the name does (the avatar's width and the gap), and the bell's glyph starts where the avatar does: the bell's target reaches back past the card's text edge. */
	.ct-sub { grid-column: 1 / -1; grid-row: 2; display: flex; align-items: center; gap: var(--space-px-7); min-width: 0; }
	/* Agree and Resolve are targets of at least 24px (the design system's small icon button) around 14px glyphs; the last glyph's right edge is the card's text edge, where the reply's Send and the sheet's close end too, so the target reaches out past the edge by its padding. */
	.ct-head { --act-inset: 5px; }
	/* Agree and Resolve have different effects, so one target's width lies between them (24px here, 44px on touch): a slip lands on empty space, never on the other one. */
	.ct-acts { grid-column: 2; grid-row: 1; display: flex; align-items: center; gap: var(--space-6); margin-right: calc(-1 * var(--act-inset)); }
	/* Mute is the bell and its word, so the word says what the bell does where there is no hover. The bell's glyph starts where the avatar does: the target reaches back past the card's text edge. */
	.ct-sub .ct-act.mute { --mute-pad: var(--space-px-3); gap: var(--space-px-5); padding-inline: var(--mute-pad) var(--space-2); margin-left: calc(-1 * var(--mute-pad)); font: inherit; font-size: var(--text-caption); font-weight: var(--fw-medium); white-space: nowrap; }
	.ct-mute-word { line-height: 1; }
	.ct-sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
	.ct-reply-count { flex: none; white-space: nowrap; display: inline-flex; align-items: center; gap: var(--space-1); font-size: var(--text-caption); color: var(--text-tertiary); }
	/* Avatar owns the shared initials presentation. */
	.ct-act { display: flex; align-items: center; justify-content: center; position: relative; min-width: var(--space-6); min-height: var(--space-6); box-sizing: border-box; padding: var(--space-px-3); border-radius: var(--radius-sm); background: none; border: none; color: var(--text-tertiary); cursor: pointer; transition: opacity var(--dur-tight), background var(--dur-tight), color var(--dur-tight); }
	/* No ring of its own: the head's actions take the browser's focus ring, as Button does, which follows the button's radius. */
	.ct-act.mute:hover, .ct-act.mute.on { color: var(--text-primary); }
	/* Hover colour is for a pointer: a tap on touch would leave it stuck on the bell. */
	@media (hover: hover) { .ct-act.mute:hover { background: var(--bg-hover); } }
	.ct-act.mute.pending { cursor: progress; background: none; }
	/* A refusal takes the place of the time, the New marker and the replies in the head's second line, so the card keeps its height; Try again is pressing Mute again. The line is 24px high (the bell's target), which holds two lines of 12px: the dropped connection's sentence and a reference wrap inside it, so nothing grows or moves. */
	.ct-mute-failed { flex: 1 1 0; min-width: 0; margin: 0; font-size: var(--text-caption); line-height: var(--space-3); }
	.ct-mute-ref { white-space: nowrap; color: var(--text-secondary); }
	.ct-res-acts .ct-act.mute { margin-left: auto; }
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

	@media (pointer: coarse) {
		.ct-act { min-width: calc(var(--space-8) - var(--space-1)); min-height: calc(var(--space-8) - var(--space-1)); align-items: center; justify-content: center; }
		/* A 44 px target is drawn over the line's own height (the targets of one line never meet the next). Its ring is drawn on ::before around what shows (the glyph, or the bell and its word), not around the whole target, so the ring clears the card's edge and the text beside it and never cuts the word. */
		.ct-head { --act-inset: var(--space-px-15); row-gap: var(--space-2); }
		.ct-act:focus-visible { outline: none; }
		.ct-act:focus-visible::before { content: ''; position: absolute; inset: 10px; border-radius: var(--radius-sm); pointer-events: none; outline: auto 1px; outline-color: AccentColor; outline: auto 1px -webkit-focus-ring-color; }
		.ct-sub .ct-act.mute:focus-visible::before { inset: 10px 5px 10px 12px; }
		.ct-acts { gap: var(--touch-target); }
		.ct-sub .ct-act.mute { --mute-pad: 15px; }
		.ct-acts, .ct-sub .ct-act.mute { margin-block: calc(-1 * var(--space-px-10)); }
	}
	/* The narrowest phones (320px) leave the name about 100px beside Agree and Resolve, which cuts "Agent Substrate"; the avatar's 26px (its width and gap) go to the name there, and the name is still the card's title and its accessible name. */
	@media (pointer: coarse) and (max-width: 22.5rem) { .ct-who :global(.avatar) { display: none; } .ct-head { column-gap: var(--space-1); } .ct-who { gap: var(--space-1); } }
	/* A phone on its side has about 150px under a highlight, so the card spends less on air there: no gap between the head's two lines (their targets sit side by side, never over each other), 2px between the card's parts and 8px of padding above and below in a comment's popover (other popovers keep theirs), so the whole card, its reply and the line that says who gets it show without scrolling. The head stays pinned. */
	@media (pointer: coarse) and (max-height: 28.125rem) {
		.ct-head { row-gap: 0; }
		.ct, .ct.flat { gap: var(--space-px-2); }
		:global(.popover:has(.ct, .composer)) { padding-block: var(--space-2); --compose-band: 0px; }
		/* When the card is taller than the room, the popover scrolls as one: the reply keeps its whole height and the reply box scrolls with it, not over it. */
		:global([data-popover]) .ct-reply-region { flex-shrink: 0; }
		:global([data-popover]) .ct-compose { position: static; }
		:global(.popover) .ct.flat > .ct-head { top: calc(-1 * var(--space-2)); margin: calc(-1 * var(--space-2)) calc(-1 * var(--space-3)) calc(-1 * var(--space-px-2)); padding: var(--space-2) var(--space-3) 0; }
	}

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
