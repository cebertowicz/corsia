<script lang="ts">
	import { Button } from '$lib/components/ui';

	/**
	 * Why an access request can't be approved: one red line and a ghost Retry, no box. The fill,
	 * script and lease request drawers all draw it from here, so its words and look live in one place.
	 * The lease drawer still passes its own `incomplete` words until its wording is settled.
	 */
	let {
		stale,
		onRetry,
		incomplete = 'This request arrived without everything needed to approve it. Retry to load it again.',
	}: {
		stale: boolean;
		onRetry: () => void;
		incomplete?: string;
	} = $props();
</script>

<div class="blocked-line" role="status">
	<span>{stale
		? 'Couldn’t check that this request is still current, so you can’t approve it yet.'
		: incomplete}</span>
	<Button variant="ghost" size="sm" onclick={onRetry}>Retry</Button>
</div>

<style>
	.blocked-line {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		color: var(--red-text);
		font-size: var(--text-footnote);
	}
	.blocked-line span { flex: 1; min-width: 0; }
</style>
