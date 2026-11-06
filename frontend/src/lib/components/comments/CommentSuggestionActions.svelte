<script lang="ts">
	import './comment-link.css';

	let { orphaned, readOnly, decisionError, onDecide }: {
		orphaned: boolean;
		readOnly: boolean;
		decisionError?: string;
		/** Settles when the decision does and never rejects: a refusal comes back as `decisionError`. */
		onDecide: (decision: 'accept' | 'reject') => Promise<void> | undefined;
	} = $props();
	let decisionBusy = $state<'accept' | 'reject' | null>(null);
	async function decide(decision: 'accept' | 'reject') {
		if (readOnly || decisionBusy || (decision === 'accept' && orphaned)) return;
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
		<button class="ct-suggestion-accept" aria-busy={decisionBusy === 'accept'} disabled={decisionBusy !== null || orphaned} title={orphaned ? 'The text this suggestion replaces has changed.' : undefined} onclick={(e) => { e.stopPropagation(); decide('accept'); }}>{decisionBusy === 'accept' ? 'Accepting…' : 'Accept'}</button>
		<button class="ct-link" aria-busy={decisionBusy === 'reject'} disabled={decisionBusy !== null} onclick={(e) => { e.stopPropagation(); decide('reject'); }}>{decisionBusy === 'reject' ? 'Rejecting…' : 'Reject'}</button>
	</div>
{/if}
{#if decisionError}<div class="ct-decision-error" role="alert">{decisionError}</div>{/if}
