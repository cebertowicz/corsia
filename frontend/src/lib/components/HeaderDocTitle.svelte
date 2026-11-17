<script lang="ts">
	import type { Snippet } from 'svelte';
	import type { CanvasHeaderTitleAction } from '$lib/stores/canvasHeader';
	import MiddleName from './MiddleName.svelte';

	let { title, action, titleEnd = null, onclick }: {
		title: string;
		/** What the name opens: the open-files list, with the accent when another open file changed. */
		action: CanvasHeaderTitleAction;
		/** The document's own mark after the ⌄ (File changed), drawn by the document. */
		titleEnd?: Snippet | null;
		onclick: () => void;
	} = $props();
	/** A document name keeps its extension, or its last four characters, when it cuts in the middle. */
	const extensionOf = (name: string) => /\.[A-Za-z0-9]{1,5}$/.exec(name)?.[0] ?? name.slice(-4);
</script>

<!-- The name and its ⌄ never leave the bar; a long name cuts in the middle and keeps its extension. -->
<button type="button" class="h-doc-title" class:changed={action.changed} data-testid="header-document-title" {onclick} aria-label={`${title} · ${action.label}`} title={action.title}>
	<span class="h-scope"><MiddleName name={title} tail={extensionOf(title)} refit={titleEnd} /></span>
	<svg aria-hidden="true" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6.5l4 4 4-4" /></svg>
</button>
{#if titleEnd}<span class="h-doc-title-end">{@render titleEnd()}</span>{/if}

<style>
	/* A document's name is the control that opens its open-files list; the ⌄ after it says so. */
	.h-doc-title { display: inline-flex; flex: 0 1 auto; align-items: center; gap: var(--space-1); min-width: 0; max-width: 100%; min-height: var(--touch-target); padding: 0; border: 0; border-radius: var(--radius-sm); background: none; color: inherit; font: inherit; cursor: pointer; }
	.h-scope { display: grid; flex: 0 1 auto; min-width: 0; overflow: hidden; white-space: nowrap; }
	.h-doc-title svg { flex: none; color: var(--text-tertiary); }
	/* Another open file changed: the ⌄ takes the accent, as the open-files control did. */
	.h-doc-title.changed svg { color: var(--accent-text); }
	.h-doc-title:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	/* File changed follows the name; its own touch target keeps it reachable. */
	.h-doc-title-end { display: inline-flex; flex: none; }
</style>
