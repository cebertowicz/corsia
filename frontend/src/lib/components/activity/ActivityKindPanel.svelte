<script lang="ts">
	import type { Snippet } from 'svelte';

	/**
	 * The pane for an item that is not a document (a decision, a task with no document, a held message, a chat reply):
	 * the same frame as the document pane, its one tab as the first row and its control docked at the bottom edge,
	 * where the document's review bar sits.
	 */
	let { label, icon = 'item', body, band }: {
		label: string;
		icon?: 'item' | 'chat';
		body: Snippet;
		/** The docked control (a ReviewBarShell in docked mode), placed against the canvas's bottom edge. */
		band?: Snippet;
	} = $props();
</script>

<div class="panel">
	<div class="panel-strip">
		<div class="strip" role="tablist" aria-label="Open item">
			<span class="tab-wrap active" role="tab" aria-selected="true" tabindex="0">
				{#if icon === 'chat'}
					<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z" /></svg>
				{:else}
					<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></svg>
				{/if}
				<span class="tab-title">{label}</span>
			</span>
		</div>
	</div>
	<div class="canvas">
		<div class="box">
			<div class="scroll">
				<div class="content">{@render body()}</div>
			</div>
			{#if band}{@render band()}{/if}
		</div>
	</div>
</div>

<style>
	.panel {
		position: relative; height: 100%; display: flex; flex-direction: column; min-width: 0; min-height: 0;
		box-sizing: border-box; padding-top: var(--header-bottom); background: var(--bg-primary);
	}
	/* The tab row sits in the header band, as the document pane's does. */
	.panel-strip {
		position: absolute; inset: 0 var(--header-right-controls-inset, 0px) auto 0;
		height: var(--header-height); z-index: 11; overflow: hidden;
	}
	.strip {
		display: flex; align-items: center; height: 100%; padding: 0 var(--space-2) 0 var(--space-3);
	}
	.tab-wrap {
		/* The document pane's tab size (ArtifactTabStrip). */
		display: flex; align-items: center; gap: var(--space-2); max-width: 16.25rem; height: var(--space-7); padding: 0 var(--space-3);
		border-radius: var(--radius-pill); background: var(--bg-active); color: var(--text-primary);
		font-size: var(--text-footnote); font-weight: var(--fw-medium);
	}
	.tab-wrap:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	.tab-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
	/* Named pads so the docked control reaches the canvas's edges, as in the document canvas. */
	.canvas {
		position: relative; flex: 1; min-height: 0; overflow: hidden;
		--canvas-pad-inline: var(--space-5); --canvas-pad-bottom: var(--space-5);
		padding: 0 var(--canvas-pad-inline) var(--canvas-pad-bottom);
	}
	/* The box the docked control is placed against, inside the canvas's padding, as the document's box is. */
	.box { position: relative; height: 100%; }
	.scroll { height: 100%; overflow-y: auto; }
	/* The docked control covers the canvas bottom; the content keeps its last lines clear of it. */
	.content { max-width: 40rem; margin: var(--space-6) auto 0; padding: 0 var(--space-2) 10rem; }
	:global(.activity.phone) .canvas { --canvas-pad-inline: var(--space-3); }
</style>
