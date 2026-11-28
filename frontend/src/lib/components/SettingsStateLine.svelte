<script lang="ts">
	import type { Snippet } from 'svelte';
	import SettingsReveal from './SettingsReveal.svelte';

	/**
	 * The line under a Settings row that says what is happening to it or what went wrong. It keeps its last words while it
	 * closes, so it shrinks with its text still on it. `alert` makes it announce itself.
	 */
	let { text, tone = 'quiet', alert = false, action }: {
		text: string;
		tone?: 'quiet' | 'attention' | 'failed';
		alert?: boolean;
		/** A text action at the line's end (Cancel). */
		action?: Snippet;
	} = $props();

	let held = $state('');
	$effect(() => { if (text) held = text; });
</script>

<SettingsReveal open={!!text}>
	<div class="line {tone}" role={alert && text ? 'alert' : undefined}>
		<span class="words">{text || held}</span>
		{#if action && text}{@render action()}{/if}
	</div>
</SettingsReveal>

<style>
	.line { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); padding: 0 var(--space-2); font-size: var(--text-caption); line-height: 1.45; color: var(--text-tertiary); }
	.words { min-width: 0; overflow-wrap: anywhere; }
	.attention { color: var(--amber-text); }
	.failed { color: var(--red-text); }
</style>
