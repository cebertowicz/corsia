<script lang="ts">
	import { Button } from '$lib/components/ui';
	import { CONSENT_CHANGES, CONSENT_NO_CHANGE, CONSENT_READS, CONSENT_READS_KEY } from '$lib/ownOmlxCopy';

	interface Props {
		/** Let CORSIA manage it was chosen on Welcome: the admin key is read too. */
		manage: boolean;
		busy: boolean;
		onDecide: (useIt: boolean) => void;
	}
	const { manage, busy, onDecide }: Props = $props();
	const reads = $derived(manage ? [...CONSENT_READS, CONSENT_READS_KEY] : CONSENT_READS);
</script>

<div class="consent">
	<p>CORSIA will read:</p>
	<ul>
		{#each reads as item}<li>{item}</li>{/each}
	</ul>
	<p>{manage ? CONSENT_CHANGES : CONSENT_NO_CHANGE}</p>
	<div class="consent-actions">
		<Button variant="chip" onclick={() => onDecide(false)} disabled={busy}>Not now</Button>
		<Button variant="chip" onclick={() => onDecide(true)} loading={busy} disabled={busy}>Use it</Button>
	</div>
</div>

<style>
	.consent { display: flex; flex-direction: column; gap: var(--space-4); max-width: calc(var(--space-8) * 9); color: var(--text-secondary); }
	.consent p { margin: 0; }
	.consent ul { margin: 0; padding-left: var(--space-5); }
	.consent-actions { display: flex; gap: var(--space-2); }
</style>
