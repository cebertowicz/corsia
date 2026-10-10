<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		variant: 'ok' | 'err' | 'info' | 'warn';
		children: Snippet;
	}

	const { variant, children }: Props = $props();
</script>

<div class="msg {variant}" role={variant === 'err' || variant === 'warn' ? 'alert' : 'status'}>
	{@render children()}
</div>

<style>
	.msg {
		padding: 8px 12px;
		border-radius: var(--radius-xs);
		font-size: var(--text-subhead);
		margin-bottom: 12px;
		border: 1px solid;
	}
	.msg.err {
		background: var(--red-bg);
		border-color: color-mix(in srgb, var(--red) 30%, transparent);
		color: var(--red);
	}
	.msg.ok {
		background: var(--green-bg);
		border-color: var(--green-border, color-mix(in srgb, var(--green) 30%, transparent));
		color: var(--green-text, var(--green));
	}
	.msg.info {
		background: var(--violet-bg);
		border-color: var(--violet-line);
		color: var(--text-primary);
	}
	.msg.warn {
		background: var(--amber-bg);
		border-color: var(--amber-line);
		color: var(--amber);
	}
</style>
