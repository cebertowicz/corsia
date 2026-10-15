<script lang="ts">
	import { Button } from '$lib/components/ui';

	let {
		replace,
		modelLabel,
		modelSize,
		busy = false,
		onDecision,
	}: {
		replace: boolean;
		modelLabel: string;
		modelSize: string;
		busy?: boolean;
		onDecision: (accept: boolean) => void;
	} = $props();
</script>

<div class="sources consent">
	{#if replace}
		<p>CORSIA can reinstall oMLX and download {modelLabel} ({modelSize}). Reinstalling replaces that oMLX and changes its settings.</p>
	{:else}
		<p>CORSIA will read:</p>
		<ul>
			<li>your oMLX port setting</li>
			<li>the installed oMLX version</li>
			<li>your oMLX server to find its available models</li>
		</ul>
		<p>It won’t change your oMLX.</p>
	{/if}
	<div class="consent-actions"><Button variant="chip" onclick={() => onDecision(false)} disabled={busy}>Not now</Button></div>
</div>

<style>
	.sources { display: flex; flex-direction: column; gap: var(--space-4); max-width: calc(var(--space-8) * 9); color: var(--text-secondary); }
	.consent p { margin: 0; }
	.consent ul { margin: 0; padding-left: var(--space-5); }
	.consent-actions { align-self: flex-start; }
	.consent-actions :global(.btn.chip) { margin-inline: calc(var(--space-2) * -1); }
	/* The deferral deliberately uses the setup accent-text chip directly. */
</style>
