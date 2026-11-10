<script lang="ts">
	import { onMount } from 'svelte';
	import { senderName, type EmailComment, type EmailMessage } from '$lib/emailDraft';
	import AdaptiveDrawer from './ui/AdaptiveDrawer.svelte';
	import Popover from './ui/Popover.svelte';
	import CommentComposer from './comments/CommentComposer.svelte';
	import './commentAnchors.css';

	// Select text in an opened earlier email, tap Comment, write, post: the selection button and the composer are the
	// ones chat messages use (ChatCommentSelection.svelte). PROTOTYPE: a posted comment is kept in the page; the real
	// one would be stored with the email id and the quote and delivered to the agent that wrote the draft.
	let { root, messages, pending = $bindable(null), onpost }: {
		root: HTMLElement | undefined;
		messages: EmailMessage[];
		/** The email and quote a comment is being written on, so the email text can mark it. */
		pending?: { messageId: string; quote: string } | null;
		onpost: (comment: EmailComment) => void;
	} = $props();

	type Pick = { messageId: string; quote: string; rect: DOMRect };
	let pick = $state<Pick | null>(null);
	let draft = $state<Pick | null>(null);
	let closing = $state(false);
	let buttonWidth = $state(0);
	let narrow = $state(false);
	let afterClose: (() => void) | null = null;
	const coarse = typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;
	const above = $derived(!!pick && !coarse && pick.rect.top > 96);
	const column = () => root?.closest<HTMLElement>('.doc-main') ?? null;
	const left = $derived.by(() => {
		if (!pick) return 0;
		const bounds = column()?.getBoundingClientRect();
		const wanted = pick.rect.right - buttonWidth;
		return bounds ? Math.max(bounds.left + 8, Math.min(wanted, bounds.right - 8 - buttonWidth)) : wanted;
	});
	const heading = $derived.by(() => {
		const message = messages.find((item) => item.id === draft?.messageId);
		return message ? `Comment on ${senderName(message.sender)}’s email` : 'New comment';
	});

	function readSelection() {
		if (draft) return;
		const selection = document.getSelection();
		if (!selection || selection.isCollapsed || !selection.rangeCount || !root) { pick = null; return; }
		const range = selection.getRangeAt(0);
		const start = range.startContainer instanceof Element ? range.startContainer : range.startContainer.parentElement;
		const text = start?.closest<HTMLElement>('.email-text');
		const row = text?.closest<HTMLElement>('[data-email-id]');
		// Only inside one email's text: a selection that runs out of it, or into another email, is not a comment target.
		if (!text || !row || !root.contains(text) || !text.contains(range.endContainer)) { pick = null; return; }
		const quote = selection.toString().trim();
		const rect = range.getBoundingClientRect();
		if (!quote || !rect.width) { pick = null; return; }
		pick = { messageId: row.dataset.emailId ?? '', quote, rect };
	}
	function begin() { draft = pick; pending = pick && { messageId: pick.messageId, quote: pick.quote }; }
	function close() {
		pick = null; draft = null; pending = null;
		window.getSelection()?.removeAllRanges();
	}
	function requestClose() { if (!closing) { afterClose = close; closing = true; } }
	function closed() { const finish = afterClose; afterClose = null; finish?.(); closing = false; }
	async function submit(text: string) {
		const current = draft;
		if (current) onpost({ messageId: current.messageId, quote: current.quote, text });
		afterClose = close;
		closing = true;
	}
	onMount(() => {
		const update = () => { narrow = window.innerWidth <= 768; };
		update();
		window.addEventListener('resize', update);
		document.addEventListener('selectionchange', readSelection);
		return () => { window.removeEventListener('resize', update); document.removeEventListener('selectionchange', readSelection); };
	});
</script>

{#if pick && !draft}
	<button bind:offsetWidth={buttonWidth} class="sel-comment" class:above class:below={!above} style="top:{above ? pick.rect.top - 6 : pick.rect.bottom + 6}px; left:{left}px" onmousedown={(event) => event.preventDefault()} onclick={begin}>
		<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>Comment
	</button>
{/if}
{#if draft}
	{#if narrow}
		<AdaptiveDrawer ariaLabel={heading} {heading} doneLabel="Close" followKeyboard open={!closing} onClose={requestClose} onClosed={closed}>
			<CommentComposer mentions={false} quote={draft.quote} placeholder="Comment…" onSubmit={submit} onCancel={requestClose} />
		</AdaptiveDrawer>
	{:else}
		<Popover anchor={draft.rect} bounds={column()} placement="bottom" open={!closing} initialFocus="textarea" surfaceRole={null} onClose={requestClose} onClosed={closed}>
			<div role="dialog" aria-label={heading}>
				<p class="for">{heading}</p>
				<CommentComposer mentions={false} quote={draft.quote} placeholder="Comment…" onSubmit={submit} onCancel={requestClose} />
			</div>
		</Popover>
	{/if}
{/if}

<style>
	.for { margin: 0 0 var(--space-2); font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-secondary); }
</style>
