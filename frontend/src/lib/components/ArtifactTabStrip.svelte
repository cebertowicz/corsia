<script lang="ts">
	import { tick } from 'svelte';
	import { isTabChanged, type ArtifactTab } from '$lib/stores/artifactWorkspace';
	import { emailDraftHeaders } from '$lib/stores/emailDraftHeaders';
	import { emailTabTitle } from '$lib/emailDraft';
	import { documentBar } from '$lib/stores/documentBar';
	import MiddleName from './MiddleName.svelte';

	let { tabs, activeKey, imageTabIds = new Set<string>(), onSelect, onClose, onCollapse }: {
		tabs: ArtifactTab[];
		/** Tabs the backend has said are images; they get the image glyph instead of the document one. */
		imageTabIds?: ReadonlySet<string>;
		activeKey: string | null;
		onSelect: (instanceId: string) => void;
		onClose: (instanceId: string) => void;
		/** Absent where the pane is the whole view (Activity), so there is nothing to collapse into. */
		onCollapse?: () => void;
	} = $props();

	let stripEl = $state<HTMLElement | undefined>(undefined);
	let tabFloor = $state(0);
	const extensionOf = (name: string) => {
		const extension = /\.[A-Za-z0-9]{1,5}$/.exec(name)?.[0] ?? '';
		return name.slice(-Math.max(4, extension.length));
	};
	$effect(() => {
		const active = tabs.find((tab) => tab.instanceId === activeKey);
		const name = active ? emailTabTitle(active.artifactId ? $emailDraftHeaders[active.artifactId] : undefined, active.title) : undefined;
		const el = stripEl;
		if (!name || !el) { tabFloor = 0; return; }
		const measure = () => {
			const title = el.querySelector<HTMLElement>('.tab-wrap.active .tab-title');
			const tab = title?.closest<HTMLElement>('.tab-wrap');
			const context = document.createElement('canvas').getContext('2d');
			if (!title || !tab || typeof context?.measureText !== 'function') return;
			context.font = getComputedStyle(title).font;
			const floor = name.length > 6 ? `${name[0]}…${name.slice(-5)}` : name;
			tabFloor = Math.ceil(context.measureText(floor).width + tab.getBoundingClientRect().width - title.getBoundingClientRect().width);
		};
		void tick().then(measure);
		void document.fonts?.ready.then(measure);
	});
	let startHidden = $state(false);
	let endHidden = $state(false);

	function refreshScrollEdges() {
		const el = stripEl;
		if (!el) return;
		startHidden = el.scrollLeft > 1;
		endHidden = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
	}

	function reducedMotion(): boolean {
		return typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches;
	}

	function revealTab(instanceId: string) {
		const tab = Array.from(stripEl?.querySelectorAll<HTMLElement>('.tab') ?? [])
			.find((element) => element.dataset.tabId === instanceId);
		tab?.scrollIntoView?.({ block: 'nearest', inline: 'nearest', behavior: reducedMotion() ? 'auto' : 'smooth' });
		requestAnimationFrame(refreshScrollEdges);
	}

	function onWheel(event: WheelEvent) {
		const el = stripEl;
		if (!el || event.deltaY === 0) return;
		event.preventDefault();
		el.scrollBy?.({ left: event.deltaY, behavior: 'auto' });
	}

	$effect(() => {
		const el = stripEl;
		if (!el) return;
		refreshScrollEdges();
		if (typeof ResizeObserver === 'undefined') return;
		const observer = new ResizeObserver(refreshScrollEdges);
		observer.observe(el);
		return () => observer.disconnect();
	});

	// Saves and load signals replace `tabs` in place; only an opened or closed tab
	// changes this, so a person scrolling the strip is not pulled back mid-save.
	const tabIds = $derived(tabs.map((tab) => tab.instanceId).join('\n'));

	$effect(() => {
		void tabIds;
		const active = activeKey;
		void tick().then(() => {
			if (active) revealTab(active);
			else refreshScrollEdges();
		});
	});

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

<div class="strip" data-document-bar-row style:--tab-floor={`${tabFloor}px`}>
	<div
		class="tabs"
		class:start-hidden={startHidden}
		class:end-hidden={endHidden}
		role="tablist"
		aria-label="Open side panel items"
		bind:this={stripEl}
		onscroll={refreshScrollEdges}
		onwheel={onWheel}
	>
		{#each tabs as t, i (t.instanceId)}
			{@const key = t.instanceId}
			{@const active = key === activeKey}
			{@const title = emailTabTitle(t.artifactId ? $emailDraftHeaders[t.artifactId] : undefined, t.title)}
			{@const changed = isTabChanged(t)}
			<div class="tab-wrap" class:active role="presentation">
				<button
					type="button"
					class="tab"
					role="tab"
					aria-selected={active}
					tabindex={active ? 0 : -1}
					data-tab-id={key}
					onclick={() => onSelect(key)}
					onfocus={() => revealTab(key)}
					onkeydown={(e) => onKeydown(e, i)}
				>
					{#if t.kind === 'browser'}
							<svg class="tab-ico" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c2.4 2.5 3.6 5.5 3.6 9S14.4 18.5 12 21M12 3C9.6 5.5 8.4 8.5 8.4 12S9.6 18.5 12 21" /></svg>
					{:else if t.kind !== 'document'}
							<svg class="tab-ico" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z" /></svg>
						{:else if t.artifactId && $emailDraftHeaders[t.artifactId]}
							<svg class="tab-ico" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" /></svg>
						{:else if imageTabIds.has(key)}
							<svg class="tab-ico" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="9" cy="10" r="1.6" /><path d="m21 16-5-5-8 9" /></svg>
						{:else}
							<svg class="tab-ico" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></svg>
						{/if}
					<span class="tab-title" {title}><MiddleName name={title} tail={extensionOf(title)} /></span>
					{#if t.status === 'error'}
						<span class="tab-err" title="Couldn't load this document">!</span>
					{:else if t.status === 'unavailable'}
						<span class="tab-err" title="This document is unavailable">!</span>
					{:else if t.status === 'readonly'}
						<span class="tab-ro" title="Read-only" aria-label="Read-only">
							<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="10" width="16" height="11" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3" /></svg>
						</span>
					{:else if changed}
						<span class="tab-dot" title="Updated elsewhere since you opened it" aria-label="Updated elsewhere"></span>
					{/if}
				</button>
				<button type="button" class="tab-close" aria-label="Close {title}" onclick={() => onClose(key)}>
					<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12" /></svg>
				</button>
			</div>
		{/each}
	</div>
	<!-- The open document's Comments and Details sit after the tabs, before Collapse. -->
	<div class="end-controls">
		{#if $documentBar}<span class="doc-tools">{@render $documentBar.tools()}</span>{/if}
		{#if onCollapse}
			<button class="collapse-btn" onclick={onCollapse} aria-label="Collapse Files" title="Collapse">
				<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6" /></svg>
			</button>
		{/if}
	</div>
</div>

<style>
	.strip {
		display: flex; align-items: center; flex-shrink: 0;
		height: var(--header-height, 56px);
		padding: 0 6px 0 10px;
		border-bottom: 1px solid var(--border-subtle);
		background: var(--glass-thin);
		/* The row's own room, not the window's, is what the published document's bar competes with, so the row is the
		 * container the bar's and the tab's narrow forms are keyed to. */
		container-type: inline-size; container-name: strip;
	}
	.tabs {
		display: flex; align-items: stretch; flex: 1; min-width: min(var(--tab-floor), 100%);
		overflow-x: auto; scrollbar-width: none;
		gap: 2px;
	}
	.tabs::-webkit-scrollbar { display: none; }
	/* Hidden tabs leave a soft edge instead of an extra overflow control. */
	.tabs.start-hidden { -webkit-mask-image: linear-gradient(to right, transparent, currentColor var(--space-4)); mask-image: linear-gradient(to right, transparent, currentColor var(--space-4)); }
	.tabs.end-hidden { -webkit-mask-image: linear-gradient(to left, transparent, currentColor var(--space-4)); mask-image: linear-gradient(to left, transparent, currentColor var(--space-4)); }
	.tabs.start-hidden.end-hidden {
		-webkit-mask-image: linear-gradient(to right, transparent, currentColor var(--space-4), currentColor calc(100% - var(--space-4)), transparent);
		mask-image: linear-gradient(to right, transparent, currentColor var(--space-4), currentColor calc(100% - var(--space-4)), transparent);
	}
	/* A tab is a pill in the transcript's pill geometry (32 px, fully round); wide enough for a typical file name. */
	.tab-wrap {
		display: flex; align-items: center; flex-shrink: 0;
		max-width: min(260px, 100%); height: 32px; margin: auto 0; border-radius: var(--radius-pill);
		color: var(--text-tertiary); font-size: var(--text-footnote); font-weight: var(--fw-medium);
	}
	/* Hover eases in; the active mark moves in the same frame as the content it labels, never fading behind it. */
	.tab-wrap:hover { background: var(--bg-hover); color: var(--text-secondary); transition: background var(--dur-tight), color var(--dur-tight); }
	.tab-wrap.active { background: var(--bg-active); color: var(--text-primary); transition: none; }
	.tab {
		display: flex; align-items: center; gap: 6px; min-width: 0; height: 100%;
		padding: 0 2px 0 12px; background: none; border: none; cursor: pointer;
		color: inherit; font: inherit;
	}
	.tab:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; border-radius: var(--radius-pill); }
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
		width: 18px; height: 18px; margin-right: 8px; border-radius: var(--radius-circle);
		background: none; border: none; cursor: pointer; color: inherit; opacity: 0.7;
	}
	.tab-close:hover { opacity: 1; background: var(--bg-hover); color: var(--red); }

	.end-controls { display: flex; align-items: center; flex: none; }
	.doc-tools { display: inline-flex; flex: none; padding-inline: var(--space-2); }
	/* The thresholds are in rem, not px: what this row needs is set by its text, so it has to move with the reader's
	 * font size. 35.5rem = 568px, 27rem = 432px at the default 16px root. */
	@container strip (max-width: 35.5rem) {
		/* 1. The bar gives way first: its side padding, then its gaps (DocViewerStatus), to the tight rung. */
		.doc-tools { padding-inline: var(--space-1); }
	}
	@container strip (max-width: 27rem) {
		/* 3. Only when the bar has nothing left to give does the tab shorten, by its own truncation, and it keeps a whole
		 * close inside the row: a tab drawn past the row's clip is painted but not clickable, and no inset gives that back. */
		.tab-wrap { flex-shrink: 1; min-width: 6rem; }
	}
	.collapse-btn {
		display: flex; align-items: center; justify-content: center; flex-shrink: 0;
		width: 28px; height: 28px; margin-left: 4px; border-radius: var(--radius-xs);
		background: none; border: none; cursor: pointer; color: var(--text-tertiary);
	}
	.collapse-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
</style>
