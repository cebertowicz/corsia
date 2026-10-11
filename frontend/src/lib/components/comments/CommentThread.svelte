<script lang="ts">
	import { slide } from 'svelte/transition';
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
		resolved: boolean;
		orphaned?: boolean;
		acked?: boolean;
		replacement?: string | null;
		decision?: 'accepted' | 'rejected' | null;
		decidedBy?: string | null;
		replies: Array<{ id?: string; author: string; authorKind?: 'user' | 'agent'; text: string; at: string }>;
		replyWaiting?: boolean;
		/** Live documents only (4.2): a row never `drawn` shows its placement on the card. */
		placement?: 'placed' | 'placing' | 'not_placed';
		drawn?: boolean;
	};
	import { userParticipant, resolveParticipant, agentTag } from '$lib/stores/participants';
	import Avatar from '$lib/components/ui/Avatar.svelte';
	import { agentColor } from '$lib/docPresence';
	import { retryPlacement } from '$lib/stores/docComments';

	let { comment, focused = false, onFocus, readOnly = false, onReply, onResolve, onAgree, onDecision, onViewed, decisionError = '', reopenReachesOwner = false, replyText = $bindable('') }: {
		comment: CommentThreadData;
		focused?: boolean;
		onFocus?: () => void;
		readOnly?: boolean;
		onReply?: (commentId: string, text: string) => Promise<void>;
		onResolve?: (commentId: string, resolved: boolean) => void;
		onAgree?: (commentId: string, agreed: boolean) => void;
		/** Settles when the decision does and never rejects: a refusal comes back as `decisionError`. */
		onDecision?: (commentId: string, decision: 'accept' | 'reject') => Promise<void>;
		onViewed?: (commentId: string, replyId: string) => Promise<void>;
		/** Why the last decision was refused. The caller owns it, so it outlives a card that moves groups. */
		decisionError?: string;
		/** The document has an owner thread, so a re-open reaches someone and the card may say so. */
		reopenReachesOwner?: boolean;
		/** The reply being typed. The caller may own it, so it outlives a card that moves groups. */
		replyText?: string;
	} = $props();

	const me = $derived($userParticipant);
	const who = (id: string, kind?: 'user' | 'agent') => resolveParticipant(id, me, kind);
	// Avatar owns the shared initials presentation.
	const author = $derived(who(comment.author, comment.authorKind));
	const avatarColor = $derived(author.kind === 'agent' && comment.authorThreadId ? agentColor(comment.authorThreadId) : author.color);

	const isSuggestion = $derived(comment.replacement !== undefined && comment.replacement !== null);
	const suggestionDiff = $derived(isSuggestion ? lineDiff(comment.anchor, comment.replacement!) : null);
	let replyPosting = $state(false);
	let decisionBusy = $state<'accept' | 'reject' | null>(null);
	let showReplies = $state(false);
	let resolvedOpen = $state(false);
	let headOpen = $state(false);
	let openReplies = $state<Set<number>>(new Set());
	let el = $state<HTMLElement>();
	// Plain variables: the stamp effect must not re-run when an attempt fails.
	let lastViewAttempt: string | null = null;
	let retryOnVisible = false;

	$effect(() => { if (focused) showReplies = true; });
	$effect(() => { if (focused && el) el.scrollIntoView({ block: 'nearest' }); });
	function stampRenderedReply() {
		const latest = comment.replies.at(-1)?.id;
		if (!comment.replyWaiting || !showReplies || !latest || document.visibilityState !== 'visible') return;
		const attempt = `${comment.id}:${latest}`;
		if (lastViewAttempt === attempt) return;
		lastViewAttempt = attempt;
		// A failed stamp leaves the marker; the next visibility change retries it.
		onViewed?.(comment.id, latest).catch((err) => {
			console.warn('Could not record the viewed reply', err);
			retryOnVisible = true;
		});
	}
	$effect(() => { comment.replyWaiting; comment.replies.length; showReplies; stampRenderedReply(); });
	function onVisibilityChange() {
		if (retryOnVisible) { retryOnVisible = false; lastViewAttempt = null; }
		stampRenderedReply();
	}

	const LONG = 240;
	const isLong = (t: string) => t.length > LONG;
	const clamp = (t: string, open: boolean) => (isLong(t) && !open ? t.slice(0, LONG).replace(/\s+\S*$/, '') + '…' : t);
	function toggleReply(i: number) { const n = new Set(openReplies); n.has(i) ? n.delete(i) : n.add(i); openReplies = n; }

	async function postReply() {
		const t = replyText.trim();
		if (readOnly || !t || replyPosting) return;
		replyPosting = true;
		try {
			await onReply?.(comment.id, t);
			replyText = ''; showReplies = true;
		} finally {
			replyPosting = false;
		}
	}
	function onKey(e: KeyboardEvent) {
		if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); postReply(); }
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

{#snippet msg(p: ReturnType<typeof who>, body: string, at: string, open: boolean, toggle: () => void)}
	<div class="ct-msg">
		<div class="ct-msg-meta">
			<span class="ct-dot" style="background:{p.color}"></span>
			<span class="ct-msg-author">{p.name}</span>
			{#if p.kind === 'agent' && agentTag(p)}<span class="ct-tag">{agentTag(p)}</span>{/if}
			<span class="ct-at">{at}</span>
		</div>
		<p class="ct-msg-body">{clamp(body, open)}</p>
		{#if isLong(body)}<button class="ct-more" onclick={(e) => { e.stopPropagation(); toggle(); }}>{open ? 'Show less' : 'Show more'}</button>{/if}
	</div>
{/snippet}
{#snippet replyMessages()}
	{#each comment.replies as r, i (i)}
		{@render msg(who(r.author, r.authorKind), r.text, r.at, openReplies.has(i), () => toggleReply(i))}
	{/each}
{/snippet}

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
				{#if comment.text}<p class="ct-msg-body">{comment.text}</p>{/if}
				{@render replyMessages()}
			</div>
		{/if}
	</div>
{:else if comment.resolved}
	<div class="ct resolved" class:focused class:read-only={readOnly} bind:this={el} role="region" aria-label="Resolved comment by {author.name}">
		<button class="ct-res-head press-tint" onclick={() => (resolvedOpen = !resolvedOpen)}>
			<svg class="ct-check" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
			<span class="ct-res-text">Resolved · {comment.replies.length + 1} message{comment.replies.length ? 's' : ''}</span>
			<svg class="ct-chev" class:open={resolvedOpen} width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6" /></svg>
		</button>
		{#if resolvedOpen}
			<div class="ct-res-body" transition:slide={{ duration: 150 }}>
				{@render msg(author, comment.text, comment.at, headOpen, () => (headOpen = !headOpen))}
				{@render replyMessages()}
				<div class="ct-res-acts">
					<button class="ct-link" disabled={readOnly} onclick={() => onResolve?.(comment.id, false)}>Re-open</button>
					{#if reopenReachesOwner}<span class="ct-res-hint">Re-open asks the agent to revisit.</span>{/if}
				</div>
			</div>
		{/if}
	</div>
{:else}
	<!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
	<div class="ct" class:focused class:acked={comment.acked} class:read-only={readOnly} class:orphaned={comment.orphaned} bind:this={el} role="region" aria-label="Comment by {author.name}" onclick={onFocus}>
		<div class="ct-head">
			<Avatar name={author.name} color={avatarColor} hidden />
			<span class="ct-author">{author.name}</span>
			{#if author.kind === 'agent' && agentTag(author)}<span class="ct-tag" title="via {author.provider} · {author.thread}">{agentTag(author)}</span>{/if}
			<span class="ct-at">{comment.at}</span>
			{#if !isSuggestion}
				<button class="ct-act ack" class:on={comment.acked} disabled={readOnly} onclick={(e) => { e.stopPropagation(); onAgree?.(comment.id, !comment.acked); }} aria-label="Agree — end the exchange" title="Agree — ends the back-and-forth">
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 11v9H4a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1z" /><path d="M7 11l4-7a2 2 0 0 1 3 2l-1 5h5a2 2 0 0 1 2 2.3l-1.2 6A2 2 0 0 1 16.8 21H7" /></svg>
				</button>
				<button class="ct-act resolve" disabled={readOnly} onclick={(e) => { e.stopPropagation(); onResolve?.(comment.id, true); }} aria-label="Resolve" title="Resolve — close the thread">
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
				</button>
			{/if}
		</div>
		{#if !comment.drawn && comment.placement === 'placing'}
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
			{#if comment.text}<p class="ct-body">{clamp(comment.text, headOpen)}</p>{/if}
			{#if !readOnly}
				<div class="ct-suggestion-actions">
					<button class="ct-suggestion-accept" aria-busy={decisionBusy === 'accept'} disabled={decisionBusy !== null || comment.orphaned} title={comment.orphaned ? 'The text this suggestion replaces has changed.' : undefined} onclick={(e) => { e.stopPropagation(); decide('accept'); }}>{decisionBusy === 'accept' ? 'Accepting…' : 'Accept'}</button>
					<button class="ct-link" aria-busy={decisionBusy === 'reject'} disabled={decisionBusy !== null} onclick={(e) => { e.stopPropagation(); decide('reject'); }}>{decisionBusy === 'reject' ? 'Rejecting…' : 'Reject'}</button>
				</div>
			{/if}
			{#if decisionError}<div class="ct-decision-error" role="alert">{decisionError}</div>{/if}
		{:else}
			{#if comment.orphaned}<div class="ct-orphan">The text this comment was on has changed</div>{/if}
			<p class="ct-body">{clamp(comment.text, headOpen)}</p>
		{/if}
		{#if isLong(comment.text)}<button class="ct-more" onclick={(e) => { e.stopPropagation(); headOpen = !headOpen; }}>{headOpen ? 'Show less' : 'Show more'}</button>{/if}

		{#if comment.replies.length}
			<button class="ct-replies-toggle" onclick={(e) => { e.stopPropagation(); showReplies = !showReplies; }}>
				{showReplies ? '⊖ Hide' : `⊕ ${comment.replies.length}`} {comment.replies.length === 1 ? 'reply' : 'replies'}
			</button>
		{/if}
		{#if showReplies && comment.replies.length}
			<div class="ct-replies" transition:slide={{ duration: 160 }}>
				{@render replyMessages()}
			</div>
		{/if}

		{#if !isSuggestion && comment.acked}
			<div class="ct-agreed"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5" /></svg> Agreed</div>
		{:else if !readOnly}
			<div class="ct-compose">
				<input class="ct-input" placeholder="Reply…" bind:value={replyText} onkeydown={onKey} onclick={(e) => e.stopPropagation()} />
				{#if replyText.trim()}
					<button class="ct-send" onclick={(e) => { e.stopPropagation(); postReply(); }} aria-label="Send reply" title="Reply">
						<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7" /></svg>
					</button>
				{/if}
			</div>
		{/if}
	</div>
{/if}

<style>
	.ct {
		background: color-mix(in srgb, var(--text-primary) 4%, transparent); border: 1px solid transparent; border-radius: var(--radius-sm);
		padding: 8px 10px; display: flex; flex-direction: column; gap: 4px;
		transition: border-color var(--dur-tight), box-shadow var(--dur-tight); cursor: pointer;
	}
	.ct.read-only .ct-act, .ct.read-only .ct-compose, .ct.read-only .ct-res-acts { display: none; }
	.ct.focused { border-color: color-mix(in srgb, var(--accent) 55%, transparent); box-shadow: var(--glass-rim), 0 0 0 3px var(--accent-subtle); }
	.ct.acked { border-color: color-mix(in srgb, var(--green) 30%, transparent); }
	.ct.orphaned { border-color: color-mix(in srgb, var(--amber) 35%, transparent); }

	.ct-head { display: flex; align-items: center; gap: 7px; }
	/* Avatar owns the shared initials presentation. */
	.ct-author { font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-secondary); }
	.ct-tag {
		font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--text-tertiary);
		background: color-mix(in srgb, var(--text-primary) 7%, transparent); border-radius: var(--radius-2xs); padding: 1px 6px; white-space: nowrap;
	}
	.ct-at { font-size: var(--text-caption); color: var(--text-tertiary); }
	.ct-placement { display: flex; align-items: center; gap: 8px; margin: 0; font-size: var(--text-caption); color: var(--text-tertiary); }
	.ct-act { display: flex; padding: 3px; border-radius: var(--radius-2xs); background: none; border: none; color: var(--text-tertiary); cursor: pointer; transition: opacity var(--dur-tight), background var(--dur-tight), color var(--dur-tight); }
	.ct-act.ack { margin-left: auto; }
	.ct-act.ack:hover { background: color-mix(in srgb, var(--green) 16%, transparent); color: var(--green); }
	.ct-act.ack.on { color: var(--green); }
	.ct-act.resolve:hover { background: var(--bg-hover); color: var(--text-primary); }

	.ct-orphan { font-size: var(--text-caption); color: var(--amber); }
	.ct-suggestion-actions { display: flex; align-items: center; gap: 10px; margin-top: 2px; }
	.ct-suggestion-accept { font: inherit; font-size: var(--text-caption); font-weight: var(--fw-semibold); color: #fff; background: var(--accent); border: none; border-radius: var(--radius-xs); padding: 5px 10px; cursor: pointer; }
	.ct-suggestion-accept:disabled, .ct-link:disabled { opacity: 0.55; cursor: not-allowed; }
	.ct-suggestion-actions button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
	.ct-suggestion-accept:disabled[aria-busy="true"] { color: var(--text-primary); background: var(--accent-bg); opacity: 1; }
	.ct-link:disabled[aria-busy="true"] { color: var(--text-primary); background: var(--accent-bg); border-radius: var(--radius-xs); padding: 2px 6px; margin: -2px -6px; opacity: 1; }
	.ct-decision-error { font-size: var(--text-caption); color: var(--red-text); }
	.ct-body { font-size: var(--text-subhead); line-height: var(--lh-normal); color: var(--text-primary); margin: 0; }
	.ct-more { align-self: flex-start; font-size: var(--text-caption); font-weight: var(--fw-medium); color: var(--accent); background: none; border: none; padding: 0; cursor: pointer; }
	.ct-more:hover { text-decoration: underline; }

	.ct-replies-toggle { align-self: flex-start; font-size: var(--text-caption); font-weight: var(--fw-medium); color: var(--accent); background: none; border: none; padding: 0; cursor: pointer; }
	.ct-replies-toggle:hover { text-decoration: underline; }
	.ct-replies { display: flex; flex-direction: column; gap: 10px; margin-top: 2px; }
	.ct-msg { display: flex; flex-direction: column; gap: 3px; }
	.ct-msg-meta { display: flex; align-items: center; gap: 6px; }
	.ct-dot { width: 7px; height: 7px; border-radius: var(--radius-circle); flex-shrink: 0; }
	.ct-msg-author { font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--text-secondary); }
	.ct-msg-body { font-size: var(--text-subhead); line-height: var(--lh-snug); color: var(--text-primary); margin: 0; }

	.ct-agreed { display: flex; align-items: center; gap: 5px; font-size: var(--text-caption); font-weight: var(--fw-medium); color: var(--green); margin-top: 2px; }

	.ct-compose { display: flex; align-items: center; gap: 6px; margin-top: 2px; }
	.ct-input {
		flex: 1; min-width: 0; font: inherit; font-size: var(--text-footnote);
		padding: 6px 10px; border-radius: var(--radius-sm); border: 1px solid var(--border); background: var(--bg-primary); color: var(--text-primary);
	}
	.ct-input:focus { outline: none; border-color: var(--accent); }
	.ct-send { flex-shrink: 0; display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: var(--radius-sm); background: var(--accent); border: none; color: #fff; cursor: pointer; transition: filter var(--dur-tight); }
	.ct-send:hover { filter: brightness(1.08); }

	.ct.resolved { padding: 0; background: none; cursor: default; gap: 0; }
	.ct-res-head { display: flex; align-items: center; gap: 8px; width: 100%; text-align: left; background: none; border: none; cursor: pointer; padding: 6px 10px; }
	.ct-check { color: var(--green); flex-shrink: 0; }
	.ct-cross { color: var(--text-secondary); flex-shrink: 0; }
	.ct-res-text { flex: 1; font-size: var(--text-footnote); color: var(--text-secondary); }
	.ct-chev { color: var(--text-tertiary); flex-shrink: 0; transition: transform var(--dur-tight); }
	.ct-chev.open { transform: rotate(90deg); }
	.ct-res-body { display: flex; flex-direction: column; gap: 8px; padding: 2px 10px 8px; }
	.ct-res-acts { display: flex; align-items: center; gap: 8px; }
	.ct-link { flex-shrink: 0; font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--accent); background: none; border: none; padding: 0; cursor: pointer; }
	.ct-link:hover { text-decoration: underline; }
	.ct-res-hint { font-size: var(--text-caption); color: var(--text-tertiary); }

</style>
