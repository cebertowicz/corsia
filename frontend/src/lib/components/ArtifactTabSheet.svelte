<!--
	The list of open side-panel items.

	Presentation, gesture, focus, history back layer, responsive geometry and
	accessibility all belong to `AdaptiveDrawer`; this component owns only the
	list and its selection/close semantics. It previously duplicated all of that
	machinery, and the duplicate drifted: its background referenced a token that
	never existed, so the browser dropped the declaration and the sheet rendered
	transparent over the content beneath it.
-->
<script lang="ts">
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';
	import { isTabChanged, type ArtifactTab } from '$lib/stores/artifactWorkspace';

	let { tabs, activeKey, onSelect, onClose, onCloseSheet }: {
		tabs: ArtifactTab[];
		activeKey: string | null;
		onSelect: (instanceId: string) => void;
		onClose: (instanceId: string) => void;
		onCloseSheet: () => void;
	} = $props();

	function pick(instanceId: string) {
		onSelect(instanceId);
		onCloseSheet();
	}
</script>

<AdaptiveDrawer ariaLabel="Open side panel items" heading="Open items" onClose={onCloseSheet}>
	<ul class="sheet-list">
		{#each tabs as t (t.instanceId)}
			<li class="sheet-row" class:active={t.instanceId === activeKey}>
				<button
					class="sheet-pick"
					type="button"
					aria-current={t.instanceId === activeKey}
					data-drawer-autofocus={t.instanceId === activeKey ? '' : undefined}
					onclick={() => pick(t.instanceId)}
				>
					<span class="sheet-ico">
						{#if t.kind !== 'document'}
							<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z" /></svg>
						{:else}
							<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></svg>
						{/if}
					</span>
					<span class="sheet-title">{t.title}</span>
					{#if t.status === 'error' || t.status === 'unavailable'}
						<span class="sheet-tag err">{t.status === 'error' ? 'Failed' : 'Unavailable'}</span>
					{:else if t.status === 'readonly'}
						<span class="sheet-tag">Read-only</span>
					{:else if isTabChanged(t)}
						<span class="sheet-dot" aria-label="Updated elsewhere"></span>
					{/if}
				</button>
				<button class="sheet-close" type="button" aria-label="Close {t.title}" onclick={() => onClose(t.instanceId)}>
					<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12" /></svg>
				</button>
			</li>
		{/each}
	</ul>
</AdaptiveDrawer>

<style>
	.sheet-list { list-style: none; margin: 0; padding: 0; }
	.sheet-row {
		display: flex; align-items: center; gap: 2px;
		border-radius: var(--radius-sm);
	}
	.sheet-row.active { background: var(--accent-bg); }
	.sheet-pick {
		flex: 1; min-width: 0; display: flex; align-items: center; gap: 10px;
		padding: 12px 8px; background: none; border: none; cursor: pointer;
		font: inherit; font-size: var(--text-body); color: var(--text-primary); text-align: left;
	}
	.sheet-row.active .sheet-pick { color: var(--accent); font-weight: var(--fw-medium); }
	.sheet-ico { display: flex; flex-shrink: 0; color: var(--text-tertiary); }
	.sheet-row.active .sheet-ico { color: var(--accent); }
	.sheet-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.sheet-dot {
		flex-shrink: 0; width: 8px; height: 8px;
		border-radius: var(--radius-circle); background: var(--accent);
	}
	.sheet-tag {
		flex-shrink: 0; font-size: var(--text-caption); color: var(--text-tertiary);
		background: color-mix(in srgb, var(--text-primary) 7%, transparent);
		border-radius: var(--radius-pill); padding: 2px 8px;
	}
	.sheet-tag.err { color: var(--red); background: var(--red-bg); }
	.sheet-close {
		flex-shrink: 0; display: flex; align-items: center; justify-content: center;
		width: 36px; height: 36px; margin-right: 4px; border-radius: var(--radius-xs);
		background: none; border: none; cursor: pointer; color: var(--text-tertiary);
	}
	.sheet-close:hover { color: var(--red); background: var(--bg-hover); }
	.sheet-pick:focus-visible, .sheet-close:focus-visible {
		outline: 2px solid var(--accent); outline-offset: -2px; border-radius: var(--radius-sm);
	}
	@media (max-width: 620px) {
		.sheet-close { width: 44px; height: 44px; }
	}
</style>
