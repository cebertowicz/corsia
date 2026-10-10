<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		title: string;
		action?: Snippet;
		children: Snippet;
	}

	const { title, action, children }: Props = $props();
</script>

<section class="section">
	<header class="head">
		<span class="title">{title}</span>
		{#if action}
			<span class="action">{@render action()}</span>
		{/if}
	</header>
	<div class="body">
		{@render children()}
	</div>
</section>

<style>
	.section { margin-top: 14px; }
	.section:first-child { margin-top: 0; }

	.head {
		display: flex; align-items: center; justify-content: space-between;
		margin-bottom: 8px;
	}
	.title {
		font-size: var(--text-caption); font-weight: 700; letter-spacing: 0.12em;
		text-transform: uppercase;
		color: var(--text-tertiary);
	}
	.action { display: flex; align-items: center; gap: 6px; }

	.body { display: contents; }
</style>
