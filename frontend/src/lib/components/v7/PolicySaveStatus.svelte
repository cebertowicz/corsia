<script lang="ts">
	interface Props { id?: string; pending?: boolean; failure?: string | null; }
	const { id, pending = false, failure = null }: Props = $props();
</script>

{#if pending}<span class="policy-status" {id} role="status">Saving…</span>{/if}
{#if failure}<span class="policy-status error" {id} role="status">{failure}</span>{/if}

<style>
	/* Each host places the line in its own grid; this owns only how it reads. */
	.policy-status { color: var(--text-secondary); font-size: var(--text-footnote); }
	.policy-status.error { color: var(--red-text); }
</style>
