<script lang="ts">
	import type { Snippet } from 'svelte';

	// One group on a Settings page: a §0.1 section label (with a project mark when
	// the group is a project's level), then its rows. Space alone separates groups.
	let { label, mark = null, anchor = null, hint = '', children }: {
		label: string;
		mark?: string | null;
		/** A `data-settings-anchor` a link can scroll to. */
		anchor?: string | null;
		hint?: string;
		children: Snippet;
	} = $props();
</script>

<section class="settings-group" aria-label={label} data-settings-anchor={anchor ?? undefined}>
	<h3 class="label">
		{#if mark}<span class="mark" style:background={mark} aria-hidden="true"></span>{/if}
		{label}
	</h3>
	{#if hint}<p class="hint">{hint}</p>{/if}
	{@render children()}
</section>

<style>
	.settings-group { display: flex; flex-direction: column; gap: var(--space-1); margin-top: var(--space-6); }
	.settings-group:first-child { margin-top: 0; }
	.label {
		display: flex; align-items: center; gap: var(--space-2);
		margin: 0; padding: 0 var(--space-2);
		font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-tertiary);
	}
	.mark {
		width: var(--project-mark-size); height: var(--project-mark-size);
		border-radius: var(--radius-circle);
		box-shadow: var(--project-mark-ring);
	}
	.hint { margin: 0 0 var(--space-1); padding: 0 var(--space-2); font-size: var(--text-caption); color: var(--text-tertiary); }
</style>
