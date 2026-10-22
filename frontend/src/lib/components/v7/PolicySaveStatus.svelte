<script lang="ts">
	import { saveFailureText } from '$lib/saveFailure';

	interface Props { id?: string; pending?: boolean; failed?: boolean; }
	const { id, pending = false, failed = false }: Props = $props();
</script>

{#if pending}<span class="policy-status" {id} role="status">Saving…</span>{/if}
{#if failed}<span class="policy-status error" {id} role="status">{saveFailureText(undefined, true)}</span>{/if}

<style>
	/* Each host places the line in its own grid; this owns only how it reads. */
	.policy-status { color: var(--text-secondary); font-size: var(--text-footnote); }
	.policy-status.error { color: var(--red-text); }
</style>
