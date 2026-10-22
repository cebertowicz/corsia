<script lang="ts">
	interface Props { message: string; error?: boolean; }
	const { message, error = false }: Props = $props();
</script>

<div class:err={error} class="phase-stub">{message}</div>

<style>
	/* A message in the nested list: on the provider's name column, in the status text colour. */
	.phase-stub {
		margin-left: calc(var(--space-2) + var(--project-mark-size) + var(--space-3));
		margin-right: var(--space-2);
		padding: var(--space-2) 0;
		font-size: var(--text-caption);
		color: var(--text-tertiary);
	}
	.phase-stub.err { color: var(--red-text); }
</style>
