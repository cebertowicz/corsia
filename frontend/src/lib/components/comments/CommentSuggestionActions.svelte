<script lang="ts">
	import './comment-link.css';
	import { IconButton, Spinner } from '$lib/components/ui';

	let { orphaned, textChanged = false, readOnly, decisionError, name = '', onDecide }: {
		orphaned: boolean;
		/** An open suggestion edited inside: it keeps its place and cannot be accepted. */
		textChanged?: boolean;
		readOnly: boolean;
		decisionError?: string;
		/** What the suggestion changes, in a few words (`suggestionName`): each button's accessible name ends with it, so cards in a list do not expose identical controls. */
		name?: string;
		/** Settles when the decision does and never rejects: a refusal comes back as `decisionError`. */
		onDecide: (decision: 'accept' | 'reject') => Promise<void> | undefined;
	} = $props();
	let decisionBusy = $state<'accept' | 'reject' | null>(null);
	const named = (verb: string) => (name ? `${verb}: ${name}` : verb);
	/** Why Accept is off, said on the screen as well as in its tooltip: a touch screen has no tooltip. */
	const cantAccept = $derived(textChanged ? 'Can’t accept: the text doesn’t match this suggestion.' : orphaned ? 'Can’t accept: the text this suggestion replaces isn’t in the document.' : null);
	const cantAcceptShort = $derived(textChanged ? 'Can’t accept: the text doesn’t match.' : 'Can’t accept: the text isn’t in the document.');
	async function decide(decision: 'accept' | 'reject') {
		if (readOnly || decisionBusy || (decision === 'accept' && (orphaned || textChanged))) return;
		decisionBusy = decision;
		try {
			await onDecide(decision);
		} finally {
			decisionBusy = null;
		}
	}
</script>

<!-- In a popover or a one-thread sheet that scrolls, this row is what stays in view at the bottom, and the reply field above it scrolls with the card (`keepInView` leaves this row's height clear): nothing sits over Accept and Reject, however short the surface. In a list of cards it is the last row of its own card and covers nothing. A failed decision shows in the same row. -->
<div class="ct-suggestion-pin" data-pin-bottom>
{#if !readOnly}
	<!-- The same icon button the comment list's Close uses (`ui/IconButton`), with its name beside the icon: a touch screen has no tooltip. The accessible name adds which suggestion it decides. While a decision is in flight the pair is disabled and the pressed one shows a spinner and "Accepting…" or "Rejecting…". On a touch screen each is a 44 px target with room between them. -->
	<div class="ct-suggestion-actions">
		<IconButton class="ct-suggestion-accept" variant="ghost" size="lg" ariaLabel={decisionBusy === 'accept' ? named('Accepting') : named('Accept')} busy={decisionBusy === 'accept'} disabled={decisionBusy !== null || orphaned || textChanged} title={cantAccept ?? named('Accept')} onclick={(e) => { e.stopPropagation(); decide('accept'); }}>
			{#if decisionBusy === 'accept'}<Spinner size={16} />{:else}<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5" /></svg>{/if}
			<span class="ct-act-label">{decisionBusy === 'accept' ? 'Accepting…' : 'Accept'}</span>
		</IconButton>
		<IconButton class="ct-link" variant="ghost" size="lg" ariaLabel={decisionBusy === 'reject' ? named('Rejecting') : named('Reject')} busy={decisionBusy === 'reject'} disabled={decisionBusy !== null} title={named('Reject')} onclick={(e) => { e.stopPropagation(); decide('reject'); }}>
			<!-- The X the decided card's "Suggestion rejected" line draws, at Accept's size and stroke. -->
			{#if decisionBusy === 'reject'}<Spinner size={16} />{:else}<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12" /></svg>{/if}
			<span class="ct-act-label">{decisionBusy === 'reject' ? 'Rejecting…' : 'Reject'}</span>
		</IconButton>
	</div>
{/if}
{#if cantAccept && !readOnly}<div class="ct-cant-accept">{cantAcceptShort}</div>{/if}
{#if decisionError}<div class="ct-decision-error" role="alert">{decisionError}</div>{/if}
</div>

<style>
	.ct-suggestion-pin { display: flex; flex-direction: column; gap: var(--space-1); }
	/* Pinned only where the card is the whole surface: a popover, or the sheet holding one thread (a flat card). A card in a list sits in the list's own scroller, where a pinned row would lie over the next card, so there it follows the card's content like any other row. */
	:global([data-popover]) .ct-suggestion-pin, :global([data-adaptive-drawer]) :global(.ct.flat) .ct-suggestion-pin { position: sticky; bottom: 0; z-index: 1; padding-block: var(--space-1); background: var(--bg-elevated); }
	:global([data-adaptive-drawer]) :global(.ct.flat) .ct-suggestion-pin { background: var(--bg-secondary); }
	.ct-suggestion-actions { display: flex; align-items: center; gap: var(--space-2); margin-top: 2px; }
	/* IconButton transitions every property; the focus ring, and the colours when the theme changes, must be final the moment they land (the plain text buttons these replace did not animate). The name sits beside the icon, so the button grows from its square to fit it. */
	.ct-suggestion-actions :global(.ico-btn.lg) { width: auto; min-width: 36px; padding-inline: var(--space-3); gap: var(--space-2); border-radius: var(--radius-sm); font-size: var(--text-footnote); font-weight: var(--fw-semibold); white-space: nowrap; transition: none; }
	.ct-suggestion-actions :global(.ico-btn.ct-suggestion-accept) { color: var(--accent-on-fill); background: var(--accent-fill); }
	/* Reject is a bare glyph and name, like Agree, Resolve and Close beside it: the filled button is the one that accepts. */
	.ct-suggestion-actions :global(.ico-btn.ct-link) { color: var(--text-secondary); background: transparent; }
	.ct-suggestion-actions :global(.ico-btn.ct-link:hover:not(:disabled)) { color: var(--text-primary); background: var(--bg-hover); text-decoration: none; }
	.ct-suggestion-actions :global(.ico-btn:disabled) { opacity: 0.55; cursor: not-allowed; }
	.ct-suggestion-actions :global(.ico-btn:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* The pressed one of the pair while its request is out: spinner and "Accepting…" or "Rejecting…" at full strength on a wash; the other is dimmed like any disabled button. */
	.ct-suggestion-actions :global(.ico-btn.ct-suggestion-accept:disabled[aria-busy="true"]), .ct-suggestion-actions :global(.ico-btn.ct-link:disabled[aria-busy="true"]) { color: var(--text-primary); background: var(--accent-wash); opacity: 1; }
	/* On a touch screen each is a full 44 px target with a whole target of room between them, so a miss lands on nothing: Accept and Reject do opposite things. */
	@media (pointer: coarse), (max-width: 768px) {
		.ct-suggestion-actions { gap: var(--space-8); }
		.ct-suggestion-actions :global(.ico-btn.lg) { min-width: var(--touch-target); height: var(--touch-target); }
	}
	.ct-decision-error { font-size: var(--text-caption); color: var(--red-text); }
	.ct-cant-accept { font-size: var(--text-footnote); color: var(--text-secondary); }
	/* A sheet is read at arm's length: its decision labels take the body size, not the popover's compact one. */
	:global([data-adaptive-drawer]) .ct-suggestion-actions :global(.ico-btn.lg) { font-size: var(--text-body); }
	:global([data-adaptive-drawer]) .ct-cant-accept { font-size: var(--text-subhead); }
</style>
