<script lang="ts">
	interface Props { message: string; error?: boolean; }
	const { message, error = false }: Props = $props();
</script>

<div class:err={error} class="phase-stub">{message}</div>

<style>
	.phase-stub {
		padding: calc(var(--space-3) + var(--space-1) / 2) calc(var(--space-4) + var(--space-1) / 2);
		font-size: var(--text-footnote);
		color: var(--text-tertiary);
	}
	.phase-stub.err { color: var(--red); }
	@media (max-width: 30rem) { .phase-stub { padding: calc(var(--space-2) + var(--space-1) / 2) var(--space-3); } }
</style>
