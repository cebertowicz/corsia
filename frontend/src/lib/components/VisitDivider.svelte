<script lang="ts">
	// Where this visit's new material starts (AS18); frozen for the visit.
	let { truncated = false }: { truncated?: boolean } = $props();
	const LABEL = $derived(truncated ? 'Since your last visit · more above' : 'Since your last visit');
</script>

<div class="visit-divider" role="separator" aria-label={LABEL} data-visit-divider>
	<span>{LABEL}</span>
</div>

<style>
	.visit-divider {
		max-width: 760px;
		margin: 4px auto 16px;
		scroll-margin-top: var(--sticky-top, 0px);
		display: flex;
		align-items: center;
		gap: 10px;
		color: var(--accent-text);
		font-size: var(--text-caption);
	}
	.visit-divider::before, .visit-divider::after {
		content: "";
		flex: 1;
		border-top: 1px solid var(--accent);
		opacity: 0.5;
	}
</style>
