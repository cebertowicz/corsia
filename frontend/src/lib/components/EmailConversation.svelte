<script lang="ts">
	import { tick } from 'svelte';
	import { slide } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { formatMoment, parseUTC } from '$lib/time';
	import { previewLine, senderAddress, senderName, type ConversationStatus, type EmailComment, type EmailMessage } from '$lib/emailDraft';
	import CommentQuote from './comments/CommentQuote.svelte';
	import EmailCommentSelection from './EmailCommentSelection.svelte';

	let { messages, repliedTo = null, status = 'ready', unreadable = 0, comments = [], onretry }: {
		/** Newest first: the emails that came after the one the draft answers, that one, then the older ones. */
		messages: EmailMessage[];
		/** The id of the email the draft answers; its row says so. */
		repliedTo?: string | null;
		/** Comments on lines of these emails; each names its email and quotes the line. */
		comments?: EmailComment[];
		status?: ConversationStatus;
		unreadable?: number;
		onretry?: () => void;
	} = $props();

	const uid = $props.id();
	let root = $state<HTMLElement>();
	let expanded = $state<Set<string>>(new Set());
	// Comments posted in this view, and the one being written. A comment names its email and quotes the line.
	let posted = $state<EmailComment[]>([]);
	let writing = $state<{ messageId: string; quote: string } | null>(null);
	const allComments = $derived([...comments, ...posted]);
	const now = new Date();

	function toggle(id: string) {
		const next = new Set(expanded);
		if (next.has(id)) next.delete(id); else next.add(id);
		expanded = next;
	}
	/** Open one earlier email and bring it into view (a note about it links here). */
	export async function reveal(id: string) {
		if (!expanded.has(id)) toggle(id);
		await tick();
		root?.querySelector(`[data-email-id="${CSS.escape(id)}"]`)?.scrollIntoView({ block: 'nearest', behavior: $prefersReducedMotion ? 'auto' : 'smooth' });
	}
	const moment = (iso: string) => formatMoment(parseUTC(iso), now, { clock: true });
	/** An email's text in pieces, each commented line (or the one being written) apart from the rest. */
	function pieces(message: EmailMessage): Array<{ text: string; comment?: EmailComment; writing?: boolean }> {
		const body = message.body.trim();
		const marks = [
			...allComments.filter((comment) => comment.messageId === message.id).map((comment) => ({ quote: comment.quote, comment, writing: false })),
			...(writing && writing.messageId === message.id ? [{ quote: writing.quote, comment: undefined, writing: true }] : []),
		].map((mark) => ({ ...mark, at: body.indexOf(mark.quote) })).filter((mark) => mark.at >= 0).sort((a, b) => a.at - b.at);
		const out: Array<{ text: string; comment?: EmailComment; writing?: boolean }> = [];
		let cursor = 0;
		for (const mark of marks) {
			if (mark.at < cursor) continue;
			out.push({ text: body.slice(cursor, mark.at) }, { text: mark.quote, comment: mark.comment, writing: mark.writing });
			cursor = mark.at + mark.quote.length;
		}
		out.push({ text: body.slice(cursor) });
		return out;
	}
	const label = $derived(
		status === 'ready' || status === 'partial'
			? `In this conversation · ${messages.length} ${messages.length === 1 ? 'email' : 'emails'}`
			: 'In this conversation',
	);
</script>

<section class="email-conversation" aria-labelledby="{uid}-label" bind:this={root}>
	<h3 class="email-conversation-label" id="{uid}-label">{label}</h3>
	{#if status === 'loading'}
		<p class="email-status" role="status">Loading the earlier emails…</p>
	{:else if status === 'error'}
		<p class="email-status failed" role="alert">
			<span>Couldn’t load the earlier emails in this conversation.</span>
			<button type="button" class="email-retry" onclick={onretry}>Try again</button>
		</p>
	{:else if status === 'partial'}
		<p class="email-status failed" role="alert">
			<span>{unreadable} {unreadable === 1 ? 'email' : 'emails'} in this conversation couldn’t be loaded.</span>
			<button type="button" class="email-retry" onclick={onretry}>Try again</button>
		</p>
	{/if}
	{#if messages.length}
		<ul class="email-list">
			{#each messages as message (message.id)}
				{@const open = expanded.has(message.id)}
				<li class="email-row" class:open data-email-id={message.id}>
					<button type="button" class="email-toggle" aria-expanded={open} aria-controls="{uid}-{message.id}" onclick={() => toggle(message.id)}>
						<span class="email-from" title={message.sender}>{senderName(message.sender)}</span>
						<span class="email-meta">
							<span class="email-date">{moment(message.date)}</span>
							<svg class="email-chevron" class:open width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>
						</span>
						{#if message.id === repliedTo}<span class="email-second">Replying to this email</span>{/if}
						{#if !open}<span class="email-second email-preview">{previewLine(message.body)}</span>{/if}
					</button>
					{#if open}
						<div class="email-body" id="{uid}-{message.id}" transition:slide={{ duration: $prefersReducedMotion ? 0 : 220, easing: cubicOut }}>
							{#if senderAddress(message.sender)}<div class="email-address">{senderAddress(message.sender)}</div>{/if}
							<div class="email-text">{#each pieces(message) as piece, at (at)}{#if piece.comment}<mark class="cm-comment-anchor email-commented">{piece.text}</mark>{:else if piece.writing}<mark class="cm-comment-pending email-commented">{piece.text}</mark>{:else}{piece.text}{/if}{/each}</div>
							{#each allComments.filter((comment) => comment.messageId === message.id) as comment (comment.quote + comment.text)}
								<div class="email-comment">
									<p class="email-comment-for">Comment on {senderName(message.sender)}’s email · {moment(message.date)}</p>
									<CommentQuote quote={comment.quote} lines={2} />
									<p class="email-comment-text">{comment.text}</p>
								</div>
							{/each}
						</div>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
	<EmailCommentSelection root={root} {messages} bind:pending={writing} onpost={(comment) => (posted = [...posted, comment])} />
</section>

<style>
	.email-conversation { padding: var(--space-3) var(--doc-phone-inset, var(--space-3)) var(--space-7) var(--doc-phone-inset, var(--doc-rail, 44px)); }
	.email-conversation-label { margin: 0 0 var(--space-2); padding: 0 var(--space-2); font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-tertiary); }
	.email-status { margin: 0 0 var(--space-2); padding: 0 var(--space-2); display: flex; flex-wrap: wrap; align-items: baseline; column-gap: var(--space-3); font-size: var(--text-footnote); line-height: 1.5; color: var(--text-tertiary); overflow-wrap: anywhere; }
	.email-status.failed { color: var(--red-text); }
	/* A row action is text that turns primary on hover (DESIGN-SYSTEM §0.1, trailing detail and row action). */
	.email-retry { all: unset; box-sizing: border-box; cursor: pointer; font-size: var(--text-caption); color: var(--text-tertiary); border-radius: var(--radius-sm); }
	.email-retry:hover { color: var(--text-primary); }
	.email-retry:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* Flat rows on the pane (DESIGN-SYSTEM §0.1 List row, §0.2 #3): the space between rows separates the emails, no rule. */
	.email-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); }
	.email-toggle {
		all: unset; box-sizing: border-box; width: 100%; min-height: var(--list-row-min-height); cursor: pointer;
		display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: baseline; column-gap: var(--space-3); row-gap: var(--space-1);
		padding: var(--space-3) var(--space-2); border-radius: var(--radius-sm); transition: background var(--dur-tight);
	}
	/* The press wash is the hover wash, so a touch screen, which has no hover, still answers a press and keeps no stuck hover. */
	@media (hover: hover) { .email-toggle:hover { background: var(--bg-hover); } }
	.email-toggle:active { background: var(--bg-hover); }
	.email-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	.email-from { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--text-subhead); font-weight: var(--fw-medium); color: var(--text-primary); }
	.email-meta { display: inline-flex; align-items: center; gap: var(--space-1); color: var(--text-tertiary); }
	.email-date { font-size: var(--text-caption); white-space: nowrap; }
	.email-chevron { flex: 0 0 auto; transition: transform var(--dur-tight); }
	.email-chevron.open { transform: rotate(90deg); }
	/* Second line (DESIGN-SYSTEM §0.1), drawn as SidebarThreadRow.svelte draws its update line. */
	.email-second { grid-column: 1 / -1; min-width: 0; font-size: var(--text-caption); line-height: 1.2; color: var(--text-tertiary); }
	.email-preview { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.email-body { padding: var(--space-1) var(--space-2) var(--space-3); overflow: hidden; }
	.email-address { font-size: var(--text-caption); color: var(--text-tertiary); margin-bottom: var(--space-2); overflow-wrap: anywhere; }
	/* Reader body: the treatment MarkdownEditor.svelte gives the document text through docTheme (cm/theme.ts): 15px, 16px at phone width. */
	/* The amber anchor is the shared comment-anchor role (commentAnchors.css); a mark only needs to keep the text colour. */
	.email-commented { color: inherit; }
	.email-comment { display: flex; flex-direction: column; gap: var(--space-1); margin: var(--space-3) 0 0; font-size: var(--text-footnote); line-height: var(--lh-normal); color: var(--text-secondary); }
	.email-comment p { margin: 0; }
	.email-comment-for { font-size: var(--text-caption); color: var(--text-tertiary); }
	.email-comment-text { color: var(--text-primary); overflow-wrap: anywhere; }
	.email-text { font-family: var(--font-sans); font-size: var(--text-body); line-height: var(--lh-relaxed); color: var(--text-primary); white-space: pre-wrap; overflow-wrap: anywhere; }
	@media (max-width: 768px) { .email-text { font-size: var(--text-callout); } }
	@media (pointer: coarse) { .email-toggle { min-height: var(--touch-target); } .email-retry { min-height: var(--touch-target); display: inline-flex; align-items: center; } }
	@media (prefers-reduced-motion: reduce) { .email-toggle, .email-chevron { transition: none; } }
</style>
