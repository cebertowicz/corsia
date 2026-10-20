<script lang="ts">
	interface Props { message: string; }
	const { message }: Props = $props();
</script>

<div class="row-msg">{message}</div>

<style>
	.row-msg {
		margin: var(--space-2) calc(var(--space-4) + var(--space-1) / 2);
		padding: calc(var(--space-1) + var(--space-1) / 2) calc(var(--space-2) + var(--space-1) / 2);
		font-size: var(--text-footnote);
		color: var(--red);
		background: var(--red-bg);
		border: 1px solid color-mix(in srgb, var(--red) 30%, transparent);
		border-radius: var(--radius-xs);
	}
</style>
