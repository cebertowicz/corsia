<script lang="ts">
	import './comment-link.css';

	let { orphaned, textChanged = false, readOnly, decisionError, onDecide }: {
		orphaned: boolean;
		/** An open suggestion edited inside: it keeps its place and cannot be accepted. */
		textChanged?: boolean;
		readOnly: boolean;
		decisionError?: string;
		/** Settles when the decision does and never rejects: a refusal comes back as `decisionError`. */
		onDecide: (decision: 'accept' | 'reject') => Promise<void> | undefined;
	} = $props();
	let decisionBusy = $state<'accept' | 'reject' | null>(null);
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

{#if !readOnly}
	<div class="ct-suggestion-actions">
		<button class="ct-suggestion-accept" aria-busy={decisionBusy === 'accept'} disabled={decisionBusy !== null || orphaned || textChanged} title={textChanged ? 'The text changed after this suggestion' : orphaned ? 'The text this suggestion replaces has changed.' : undefined} onclick={(e) => { e.stopPropagation(); decide('accept'); }}>{decisionBusy === 'accept' ? 'Accepting…' : 'Accept'}</button>
		<button class="ct-link" aria-busy={decisionBusy === 'reject'} disabled={decisionBusy !== null} onclick={(e) => { e.stopPropagation(); decide('reject'); }}>{decisionBusy === 'reject' ? 'Rejecting…' : 'Reject'}</button>
	</div>
{/if}
{#if decisionError}<div class="ct-decision-error" role="alert">{decisionError}</div>{/if}

<style>
	.ct-suggestion-actions { display: flex; align-items: center; gap: 10px; margin-top: 2px; }
	.ct-suggestion-accept { font: inherit; font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--accent-on-fill); background: var(--accent-fill); border: none; border-radius: var(--radius-xs); padding: 5px 10px; cursor: pointer; }
	.ct-suggestion-accept:disabled, .ct-link:disabled { opacity: 0.55; cursor: not-allowed; }
	.ct-suggestion-actions button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
	.ct-suggestion-accept:disabled[aria-busy="true"] { color: var(--text-primary); background: var(--accent-wash); opacity: 1; }
	.ct-link:disabled[aria-busy="true"] { color: var(--text-primary); background: var(--accent-wash); border-radius: var(--radius-xs); padding: 2px 6px; margin: -2px -6px; opacity: 1; }
	.ct-decision-error { font-size: var(--text-caption); color: var(--red-text); }
</style>
