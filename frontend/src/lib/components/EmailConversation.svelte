<script lang="ts">
	import { slide } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { formatMoment, parseUTC } from '$lib/time';
	import ErrorReference from '$lib/components/ui/ErrorReference.svelte';
	import { previewLine, senderAddress, senderName, type ConversationFailure, type ConversationStatus, type EmailMessage } from '$lib/emailDraft';

	let { messages, repliedTo = null, status = 'ready', reason = 'unreadable', reference, detail, onretry }: {
		/** Newest first: the emails that came after the one the draft answers, that one, then the older ones. */
		messages: EmailMessage[];
		/** The id of the email the draft answers; its row says so. */
		repliedTo?: string | null;
		status?: ConversationStatus;
		/** With `error`: why the read failed. Only a failed read can be retried from here; the others need the person elsewhere. */
		reason?: ConversationFailure;
		/** With `account`: which known cause the account reported, in its own fixed words. */
		detail?: string;
		/** With `unreadable`: the reference the failure was logged under (copy C10). */
		reference?: string;
		onretry?: () => void;
	} = $props();

	/** One message per cause, each saying what to do. */
	const FAILURE: Record<ConversationFailure, string> = {
		account: 'Couldn’t use this draft’s email account. Check it in Settings › Connected apps.',
		signin: 'You’re signed out of CORSIA. Sign in again to see the earlier emails.',
		missing: 'Couldn’t find this email draft. Ask the agent that wrote it to save it again.',
		unreadable: 'Couldn’t load the earlier emails in this conversation.',
	};

	const uid = $props.id();
	let expanded = $state<Set<string>>(new Set());
	const now = new Date();

	function toggle(id: string) {
		const next = new Set(expanded);
		if (next.has(id)) next.delete(id); else next.add(id);
		expanded = next;
	}
	const moment = (iso: string) => formatMoment(parseUTC(iso), now, { clock: true });
	const label = $derived(
		status === 'ready'
			? `In this conversation · ${messages.length} ${messages.length === 1 ? 'email' : 'emails'}`
			: 'In this conversation',
	);
</script>

<section class="email-conversation" aria-labelledby="{uid}-label">
	<h3 class="email-conversation-label" id="{uid}-label">{label}</h3>
	{#if status === 'loading'}
		<p class="email-status" role="status">Loading the earlier emails…</p>
	{:else if status === 'error'}
		<p class="email-status failed" role="alert">
			<span>{FAILURE[reason]}</span>
			{#if reason === 'unreadable'}<button type="button" class="email-retry" onclick={onretry}>Retry</button>{/if}
		</p>
		{#if reason === 'account' && detail}<p class="email-status">{detail}</p>{/if}
		{#if reason === 'unreadable' && reference}<ErrorReference {reference} />{/if}
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
							<div class="email-text">{message.body.trim()}</div>
						</div>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</section>

<style>
	.email-conversation { padding: var(--space-3) var(--doc-phone-inset, var(--space-3)) var(--space-7) calc(var(--doc-inset, 24px) - var(--space-2)); }
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
	.email-text { font-family: var(--font-sans); font-size: var(--text-body); line-height: var(--lh-relaxed); color: var(--text-primary); white-space: pre-wrap; overflow-wrap: anywhere; }
	@media (max-width: 48rem) { .email-text { font-size: var(--text-callout); } }
	@media (pointer: coarse) { .email-toggle { min-height: var(--touch-target); } .email-retry { min-height: var(--touch-target); display: inline-flex; align-items: center; } }
	@media (prefers-reduced-motion: reduce) { .email-toggle, .email-chevron { transition: none; } }
</style>
