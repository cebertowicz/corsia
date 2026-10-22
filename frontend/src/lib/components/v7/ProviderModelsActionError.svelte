<script lang="ts">
	interface Props { message: string; }
	const { message }: Props = $props();
</script>

<div class="row-msg">{message}</div>

<style>
	/* A failure is stated in the status text colour, not a box. */
	.row-msg {
		margin-left: calc(var(--space-2) + var(--project-mark-size) + var(--space-3));
		margin-right: var(--space-2);
		padding: var(--space-1) 0;
		font-size: var(--text-caption);
		color: var(--red-text);
	}
</style>
