<script lang="ts">
	import { activeThread, activeThreadId, isNavAgent } from '$lib/stores/threads';
	import { projectSettingsOpen } from '$lib/stores/projectSettings';
	import { threadSettingsOpen } from '$lib/stores/threadSettings';
	import { workspaceSettingsOpen } from '$lib/stores/workspaceSettings';
	import { sidebarCollapsed } from '$lib/stores/ui';
	import { uiScope } from '$lib/stores/uiScope';
	import { measurementScopeTitle } from '$lib/stores/measurementScopeTitle';
	import { canvasHeader } from '$lib/stores/canvasHeader';
	import { haptic } from '$lib/haptic';
	import { toastTopSurface } from '$lib/stores/toastTop';
	import { openMeasurementFromThread } from '$lib/stores/measurementNavigation';
	import type { View } from '$lib/homePaneConfig';

	let { onToggleSidebar, onTogglePane, onMeasurement, activeView, conductorThreadId, hasPane = true }: {
		onToggleSidebar: () => void;
		onTogglePane: () => void;
		/** Opens the Measurement surface; the doorway writes its own scope first (PL-2). */
		onMeasurement?: () => void;
		activeView: View;
		conductorThreadId: string | null;
		/** False where the view has no right pane; the panel toggle would open nothing. */
		hasPane?: boolean;
	} = $props();

	const h = $derived($canvasHeader);
	const isTakeover = $derived(h != null);
	const scopeTitle = $derived($uiScope?.name ?? 'Workspace');
	const designatedName = $derived($uiScope ? 'Conductor' : 'CORSIA');
	const isConductorThread = $derived(conductorThreadId != null && $activeThreadId === conductorThreadId);

	// isConductor only when no thread is active at all (home/conductor landing).
	// When activeThreadId is set but activeThread is null we are loading or
	// viewing a subagent thread (hidden from the threads store) — show neutral
	// scope title instead of the misleading "conductor" label during that gap.
	const isConductor = $derived(!isTakeover && $activeThread == null && $activeThreadId == null);

	let overflowOpen = $state(false);
	const overflowActions = $derived((h?.actions ?? []).filter((a) => !a.primary && !a.wideOnly));
	// PL-2: an agent thread opens its own scope, a conductor thread its project.
	const isLaneThread = $derived($activeThread != null && isNavAgent($activeThread));
	const showMeasures = $derived(activeView === 'chat' && !isTakeover && (isConductorThread || isLaneThread));
	const hasOverflow = $derived(overflowActions.length > 0);
	const measuresLabel = $derived(`Measurement for ${isConductorThread ? scopeTitle : ($activeThread?.title ?? 'this agent')}`);

	function openMeasures() {
		haptic();
		const threadId = $activeThread?.id ?? conductorThreadId ?? null;
		if (threadId) {
			openMeasurementFromThread({
				threadId,
				agentId: isConductorThread ? null : ($activeThread?.id ?? null),
				projectId: $activeThread?.project_id ?? $uiScope?.id ?? null,
				isConductor: isConductorThread,
			});
		}
		onMeasurement?.();
	}

	function toggleSidebar() { haptic(); onToggleSidebar(); }
	function back() { haptic(); overflowOpen = false; h?.onBack?.(); }
	function run(fn: () => void) { overflowOpen = false; fn(); }
	function openThreadSettings() {
		haptic();
		projectSettingsOpen.set(false);
		workspaceSettingsOpen.set(false);
		threadSettingsOpen.update((open) => !open);
	}
	function openProjectSettings() {
		haptic();
		threadSettingsOpen.set(false);
		workspaceSettingsOpen.set(false);
		projectSettingsOpen.set(true);
	}
	function openWorkspaceSettings() {
		haptic();
		threadSettingsOpen.set(false);
		projectSettingsOpen.set(false);
		workspaceSettingsOpen.set(true);
	}
</script>

{#snippet gearIcon()}
	<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
		<circle cx="12" cy="12" r="3"/>
		<path d="M19.4 15a1.8 1.8 0 0 0 .36 2l.04.04a2.1 2.1 0 0 1-3 3l-.04-.04a1.8 1.8 0 0 0-2-.36 1.8 1.8 0 0 0-1.08 1.64V21a2.1 2.1 0 0 1-4.2 0v-.06A1.8 1.8 0 0 0 8.4 19.3a1.8 1.8 0 0 0-2 .36l-.04.04a2.1 2.1 0 0 1-3-3l.04-.04a1.8 1.8 0 0 0 .36-2 1.8 1.8 0 0 0-1.64-1.08H2a2.1 2.1 0 0 1 0-4.2h.06A1.8 1.8 0 0 0 3.7 8.3a1.8 1.8 0 0 0-.36-2l-.04-.04a2.1 2.1 0 0 1 3-3l.04.04a1.8 1.8 0 0 0 2 .36h.02A1.8 1.8 0 0 0 9.4 2.02V2a2.1 2.1 0 0 1 4.2 0v.06a1.8 1.8 0 0 0 1.08 1.64 1.8 1.8 0 0 0 2-.36l.04-.04a2.1 2.1 0 0 1 3 3l-.04.04a1.8 1.8 0 0 0-.36 2v.02A1.8 1.8 0 0 0 21 9.4h.06a2.1 2.1 0 0 1 0 4.2H21A1.8 1.8 0 0 0 19.4 15z"/>
	</svg>
{/snippet}

{#snippet titleInner()}
	{#if isTakeover}
		<span class="h-scope">{h?.title}</span>
		{#if h?.tag}<span class="h-cond-tag">{h.tag}</span>{/if}

	{:else if activeView === 'measurement'}
		<!-- The page is titled by its scope; the thread open before it is not its scope. -->
		<span class="h-scope">{$measurementScopeTitle}</span>
	{:else if isConductor}
		<svg class="h-cond-glyph" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.8 5 5 1.8-5 1.8L12 17l-1.8-5-5-1.8 5-1.8z"/></svg>
		<span class="h-scope">{scopeTitle}</span>
		<span class="h-cond-tag">{$uiScope ? 'conductor' : 'CORSIA'}</span>
	{:else if $activeThread}
		{#if isNavAgent($activeThread)}
			<svg class="h-cond-glyph" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="Agent"><path d="M12 3l1.8 5 5 1.8-5 1.8L12 17l-1.8-5-5-1.8 5-1.8z"/></svg>
		{/if}
		<span class="h-scope">{$activeThread.title}</span>
	{:else}
		<span class="h-scope">{scopeTitle}</span>
	{/if}
{/snippet}

<header class="shell-header" class:shell-inset={$sidebarCollapsed} data-tauri-drag-region use:toastTopSurface>
	<div class="h-leading">
		<div class="h-zone h-left">
			<button class="h-icon" onclick={toggleSidebar} title="Toggle sidebar" aria-label="Toggle sidebar">
				<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><line x1="9" y1="4" x2="9" y2="20"/></svg>
			</button>
			{#if isTakeover && h?.onBack}
				<button class="h-back" onclick={back} title={h?.backLabel ?? 'Back'} aria-label={h?.backLabel ?? 'Back'}>
					<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
					<span class="h-back-label">{h?.backLabel ?? 'Back'}</span>
				</button>
			{/if}
		</div>

		<span class="h-title">{@render titleInner()}</span>
	</div>

	<div class="h-zone h-right">
		{#if isTakeover && h?.actions?.length}
			{#each h.actions as a (a.id)}
				<button class="h-act" class:active={a.active} class:overflowable={!a.primary} class:wide-only={a.wideOnly} onclick={() => run(a.onClick)} title={a.title ?? a.label}>{a.label}</button>
			{/each}
		{/if}
		{#if showMeasures}
			<button class="h-icon" onclick={openMeasures} title={measuresLabel} aria-label={measuresLabel}>
				<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18M7 16l4-5 3 3 4-7"/></svg>
			</button>
		{/if}
		{#if hasOverflow}
			<button class="h-overflow" class:open={overflowOpen} onclick={() => (overflowOpen = !overflowOpen)} aria-label="More actions" title="More">
				<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/></svg>
			</button>
		{/if}
		{#if activeView === 'chat' && !isTakeover && ($activeThread || isConductorThread)}
			<button class="h-icon" class:active={$threadSettingsOpen} onclick={openThreadSettings} title={isConductorThread ? `${designatedName} settings` : 'Thread settings'} aria-label={isConductorThread ? `${designatedName} settings` : 'Thread settings'}>
				{@render gearIcon()}
			</button>
		{:else if activeView === 'activity' && $uiScope && !$projectSettingsOpen}
			<button class="h-icon" onclick={openProjectSettings} title="Project settings" aria-label="Project settings">
				{@render gearIcon()}
			</button>
		{:else if activeView === 'activity' && !$uiScope && !$workspaceSettingsOpen}
			<button class="h-icon" onclick={openWorkspaceSettings} title="Workspace settings" aria-label="Workspace settings">
				{@render gearIcon()}
			</button>
		{/if}
		{#if hasPane}
			<button class="h-icon" onclick={onTogglePane} title="Toggle panel" aria-label="Toggle panel">
				<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><line x1="15" y1="4" x2="15" y2="20"/></svg>
			</button>
		{/if}
	</div>
</header>

{#if overflowOpen && hasOverflow}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<button class="h-overflow-backdrop" onclick={() => (overflowOpen = false)} aria-label="Close menu"></button>
	<div class="h-overflow-sheet">
		{#each overflowActions as a (a.id)}
			<button class="h-ov-item press-tint" class:active={a.active} onclick={() => run(a.onClick)}>{a.label}</button>
		{/each}
	</div>
{/if}

<style>
	.shell-header {
		position: absolute; top: 0; left: 0; right: 0;
		z-index: 10;
		height: var(--header-height);
		--header-inline-end: 12px;
		padding: 0 var(--header-inline-end) 0 max(12px, env(titlebar-area-x, 12px));
		display: flex; align-items: center; gap: 8px;
		flex-shrink: 0;
		box-shadow: var(--glass-rim);
		-webkit-app-region: drag; app-region: drag;
	}
	.shell-header::before {
		content: ''; position: absolute; inset: 0; z-index: -1; pointer-events: none;
		background-image: linear-gradient(
			180deg,
			var(--header-gradient-top) 0%,
			var(--glass-thin) 50%,
			color-mix(in srgb, var(--glass-thin) 50%, transparent) 80%,
			transparent 100%
		);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
		-webkit-mask-image: linear-gradient(180deg, rgba(0,0,0,1) 0%, rgba(0,0,0,0.95) 50%, rgba(0,0,0,0.5) 80%, transparent 100%);
		mask-image: linear-gradient(180deg, rgba(0,0,0,1) 0%, rgba(0,0,0,0.95) 50%, rgba(0,0,0,0.5) 80%, transparent 100%);
	}
	:global(html[data-shell="tauri"]) .shell-header.shell-inset { padding-left: 99px; }
	.shell-header :global(button) { -webkit-app-region: no-drag; app-region: no-drag; }

	.h-zone { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
	.h-leading { display: flex; align-items: center; min-width: 0; flex: 1; gap: 8px; }
	.h-right { margin-left: auto; }

	.h-icon {
		display: flex; align-items: center; justify-content: center;
		width: 36px; height: 36px; border-radius: var(--radius-sm);
		background: none; border: none; cursor: pointer; color: var(--text-secondary);
		transition: background var(--dur-tight), color var(--dur-tight);
	}
	.h-icon:hover { background: var(--bg-hover); color: var(--text-primary); }
	.h-icon.active { background: var(--violet-bg); color: var(--violet-text); }
	.h-icon:active { scale: 0.94; }

	.h-back {
		display: flex; align-items: center; gap: 3px; flex-shrink: 0;
		max-width: 180px;
		padding: 6px 10px 6px 6px; border-radius: var(--radius-sm);
		background: none; border: none; cursor: pointer; color: var(--text-secondary);
		font-size: var(--text-subhead); font-weight: var(--fw-medium);
		transition: background var(--dur-tight), color var(--dur-tight);
	}
	.h-back:hover { background: var(--bg-hover); color: var(--text-primary); }
	.h-back svg { flex-shrink: 0; }
	.h-back-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

	.h-title {
		flex: 1; min-width: 0;
		display: inline-flex; align-items: center; gap: 5px;
		font-size: var(--text-body); font-weight: var(--fw-semibold);
		letter-spacing: var(--ls-title); color: var(--text-primary);
		white-space: nowrap; overflow: hidden;
	}
	.h-scope { overflow: hidden; text-overflow: ellipsis; }
	/* An expanded document occupies the right canvas column. Keep the chat title
	   inside its left column, with a visible gap before the document tab strip.
	   ChatArtifactSplit publishes the actual computed column width on main. */
	:global(.app:has(.leaf-col:not(.rail):not(.full))) .h-leading {
		/* `main` owns the inherited inset. Its right padding is outside this
		   flex content box, so add it back when reserving the artifact column. */
		flex: 0 1 calc(100% - var(--artifact-header-inset) + var(--header-inline-end));
	}
	:global(.app:has(.leaf-col:not(.rail):not(.full))) .h-title {
		margin-right: var(--space-2);
	}
	.h-cond-glyph { color: var(--accent-scope-text); flex-shrink: 0; }
	.h-cond-tag { color: var(--text-tertiary); font-weight: var(--fw-medium); flex-shrink: 0; }
	.h-cond-tag::before { content: '· '; }

	.h-act {
		padding: 5px 11px; border-radius: var(--radius-sm);
		background: none; border: none; cursor: pointer; color: var(--text-secondary);
		font-size: var(--text-footnote); font-weight: var(--fw-medium);
		white-space: nowrap; transition: background var(--dur-tight), color var(--dur-tight);
	}
	.h-act:hover { background: var(--bg-hover); color: var(--text-primary); }
	.h-act.active { background: var(--accent-subtle, color-mix(in srgb, var(--accent) 14%, transparent)); color: var(--accent-scope-text); }

	.h-overflow { display: none; align-items: center; justify-content: center; width: 34px; height: 32px; border-radius: var(--radius-sm); background: none; border: none; cursor: pointer; color: var(--text-secondary); transition: background var(--dur-tight), color var(--dur-tight); }
	.h-overflow:hover, .h-overflow.open { background: var(--bg-hover); color: var(--text-primary); }
	.h-overflow-backdrop { position: fixed; inset: 0; z-index: 59; background: transparent; border: none; }
	.h-overflow-sheet {
		position: absolute; top: calc(var(--header-bottom) - 4px); right: 10px; z-index: 60;
		min-width: 168px; padding: 6px; display: flex; flex-direction: column; gap: 1px;
		background: var(--glass-regular-thick, var(--bg-secondary));
		backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
		border: 1px solid var(--border-subtle); border-radius: var(--radius);
		box-shadow: var(--glass-rim), var(--shadow-chip, 0 8px 32px rgba(0,0,0,0.4));
		animation: hSheetIn var(--dur-tight) ease-out;
	}
	@keyframes hSheetIn { from { opacity: 0; transform: translateY(-6px); } to { opacity: 1; transform: translateY(0); } }
	.h-ov-item { text-align: left; padding: 9px 11px; border-radius: var(--radius-xs); background: none; border: none; cursor: pointer; color: var(--text-secondary); font-size: var(--text-subhead); font-weight: var(--fw-medium); transition: background var(--dur-tight), color var(--dur-tight); }
	.h-ov-item:hover { background: var(--bg-hover); color: var(--text-primary); }
	.h-ov-item.active { color: var(--accent-scope-text); }

	@media (max-width: 1100px) { .h-act.wide-only { display: none; } }

	@media (max-width: 768px) {
		.shell-header {
			transform: translateY(var(--empty-parallax-offset, 0));
			height: var(--header-bottom);
			/* Moves the row down by exactly the clearance --header-bottom adds (app.css), so it
			   keeps its place relative to the bottom edge, below the iOS 27 top-edge blur.
			   ::before reads it to find the row. */
			--header-pad-top: max(8px, calc(var(--safe-top) - 28px + var(--edge-blur-clearance)));
			padding-top: var(--header-pad-top);
			padding-left: max(10px, env(safe-area-inset-left, 0px));
			padding-right: max(10px, env(safe-area-inset-right, 0px));
			box-shadow: none;
			display: grid; grid-template-columns: auto 1fr auto; gap: 4px;
		}
		/* The blur stays on this masked layer so it fades out with no hard edge.
		   Wherever the mask is below 1 the transcript shows through, so the mask stays opaque
		   down to the bottom of the row's glyphs and then fades out linearly to the header
		   bottom, over the same length as the fade below the row had before. The row is
		   centred in the box below the padding, at 50% + pad / 2, and its 18px icons and the
		   title end 9px below that centre.
		   The tint keeps its old profile below the clearance strip: the strip (0 to c) stays
		   at the first stop, and the old 50% and 80% stops of the old height (100% - c) sit
		   at c + 50% and c + 80% of it, i.e. 50% + c/2 and 80% + c/5. With no top inset c is
		   0 and these are the old tint stops. */
		.shell-header::before {
			background-image: linear-gradient(
				180deg,
				color-mix(in srgb, var(--header-top-base) 70%, transparent) var(--edge-blur-clearance),
				color-mix(in srgb, var(--header-top-base) 45%, transparent) calc(50% + var(--edge-blur-clearance) / 2),
				color-mix(in srgb, var(--header-top-base) 15%, transparent) calc(80% + var(--edge-blur-clearance) / 5),
				transparent 100%
			);
			-webkit-mask-image: linear-gradient(180deg, rgba(0,0,0,1) calc(50% + var(--header-pad-top) / 2 + 9px), transparent 100%);
			mask-image: linear-gradient(180deg, rgba(0,0,0,1) calc(50% + var(--header-pad-top) / 2 + 9px), transparent 100%);
		}
		.h-left { justify-self: start; }
		.h-right { justify-self: end; margin-left: 0; }
		/* inline-flex is content-sized; override so the 1fr cell bounds the title and
		   the existing overflow:hidden + text-overflow:ellipsis on .h-scope fires. */
		.h-title { justify-self: center; text-align: center; width: 100%; min-width: 0; }
		.h-icon { width: 44px; height: 44px; }
		.h-back-label { display: none; }
		.h-act.overflowable { display: none; }
		.h-overflow { display: flex; }
	}
</style>
