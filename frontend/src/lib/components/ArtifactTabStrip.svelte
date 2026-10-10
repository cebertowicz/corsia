<script lang="ts">
	import { isTabChanged, type ArtifactTab } from '$lib/stores/artifactWorkspace';

	let { tabs, activeKey, imageTabIds = new Set<string>(), onSelect, onClose, onCollapse }: {
		tabs: ArtifactTab[];
		/** Tabs the backend has said are images; they get the image glyph instead of the document one. */
		imageTabIds?: ReadonlySet<string>;
		activeKey: string | null;
		onSelect: (instanceId: string) => void;
		onClose: (instanceId: string) => void;
		onCollapse: () => void;
	} = $props();

	let stripEl = $state<HTMLElement | undefined>(undefined);

	function onKeydown(e: KeyboardEvent, idx: number) {
		if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
		if (tabs.length < 2) return;
		e.preventDefault();
		const dir = e.key === 'ArrowRight' ? 1 : -1;
		const next = (idx + dir + tabs.length) % tabs.length;
		onSelect(tabs[next].instanceId);
		const el = stripEl?.querySelectorAll<HTMLElement>('.tab')[next];
		el?.focus();
	}
</script>

<div class="strip">
	<div class="tabs" role="tablist" aria-label="Open side panel items" bind:this={stripEl}>
		{#each tabs as t, i (t.instanceId)}
			{@const key = t.instanceId}
			{@const active = key === activeKey}
			{@const changed = isTabChanged(t)}
			<div class="tab-wrap" class:active role="presentation">
				<button
					type="button"
					class="tab"
					role="tab"
					aria-selected={active}
					tabindex={active ? 0 : -1}
					onclick={() => onSelect(key)}
					onkeydown={(e) => onKeydown(e, i)}
				>
					{#if t.kind !== 'document'}
							<svg class="tab-ico" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z" /></svg>
						{:else if imageTabIds.has(key)}
							<svg class="tab-ico" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="9" cy="10" r="1.6" /><path d="m21 16-5-5-8 9" /></svg>
						{:else}
							<svg class="tab-ico" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></svg>
						{/if}
					<span class="tab-title" title={t.title}>{t.title}</span>
					{#if t.status === 'error'}
						<span class="tab-err" title="Couldn't load this document">!</span>
					{:else if t.status === 'unavailable'}
						<span class="tab-err" title="This document is no longer available">!</span>
					{:else if t.status === 'readonly'}
						<span class="tab-ro" title="Read-only" aria-label="Read-only">
							<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="10" width="16" height="11" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3" /></svg>
						</span>
					{:else if changed}
						<span class="tab-dot" title="Updated elsewhere since you opened it" aria-label="Updated elsewhere"></span>
					{/if}
				</button>
				<button type="button" class="tab-close" aria-label="Close {t.title}" onclick={() => onClose(key)}>
					<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12" /></svg>
				</button>
			</div>
		{/each}
	</div>
	<button class="collapse-btn" onclick={onCollapse} aria-label="Collapse artifact panel" title="Collapse">
		<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6" /></svg>
	</button>
</div>

<style>
	.strip {
		display: flex; align-items: center; flex-shrink: 0;
		height: var(--header-height, 56px);
		padding: 0 6px 0 10px;
		border-bottom: 1px solid var(--border-subtle);
		background: var(--glass-thin);
	}
	.tabs {
		display: flex; align-items: stretch; flex: 1; min-width: 0;
		overflow-x: auto; scrollbar-width: none;
		gap: 2px;
	}
	.tabs::-webkit-scrollbar { display: none; }
	.tab-wrap {
		display: flex; align-items: center; flex-shrink: 0;
		max-width: 200px; margin: 6px 0; border-radius: var(--radius-sm);
		color: var(--text-tertiary); font-size: var(--text-footnote); font-weight: var(--fw-medium);
		transition: background var(--dur-tight), color var(--dur-tight);
	}
	.tab-wrap:hover { background: var(--bg-hover); color: var(--text-secondary); }
	.tab-wrap.active { background: var(--bg-active); color: var(--text-primary); }
	.tab {
		display: flex; align-items: center; gap: 6px; min-width: 0; height: 100%;
		padding: 0 2px 0 10px; background: none; border: none; cursor: pointer;
		color: inherit; font: inherit;
	}
	.tab:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; border-radius: var(--radius-sm); }
	.tab-ico { flex-shrink: 0; }
	.tab-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
	.tab-err {
		flex-shrink: 0; width: 14px; height: 14px; border-radius: var(--radius-circle);
		display: flex; align-items: center; justify-content: center;
		font-size: 10px; font-weight: var(--fw-bold); color: var(--red); background: var(--red-bg);
	}
	.tab-dot {
		flex-shrink: 0; width: 7px; height: 7px; border-radius: var(--radius-circle);
		background: var(--accent);
	}
	.tab-ro { flex-shrink: 0; display: flex; color: var(--text-tertiary); }
	.tab-close {
		display: flex; align-items: center; justify-content: center; flex-shrink: 0;
		width: 18px; height: 18px; margin-right: 6px; border-radius: var(--radius-2xs);
		background: none; border: none; cursor: pointer; color: inherit; opacity: 0.7;
	}
	.tab-close:hover { opacity: 1; background: var(--bg-hover); color: var(--red); }

	.collapse-btn {
		display: flex; align-items: center; justify-content: center; flex-shrink: 0;
		width: 28px; height: 28px; margin-left: 4px; border-radius: var(--radius-xs);
		background: none; border: none; cursor: pointer; color: var(--text-tertiary);
	}
	.collapse-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
</style>
