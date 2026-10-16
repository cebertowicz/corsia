<script lang="ts">
	import { uiScope, enterScope, exitScope } from '$lib/stores/uiScope';
	import { paneOpen, paneDragging } from '$lib/stores/paneOpen';
	import { sidebarCollapsed } from '$lib/stores/ui';
	import { projectAccent } from '$lib/utils/projectColors';
	import { fly } from 'svelte/transition';
	import { backOut } from 'svelte/easing';

	let { activeProjects = [], onNewChat, sidebarOpen = true }: {
		activeProjects?: Array<{ id: string; name: string; status: string; accent_color?: string | null }>;
		onNewChat?: () => void;
		sidebarOpen?: boolean;
	} = $props();

	const HOLD_MS = 350;
	let scopePillsOpen = $state(false);
	let holdTimer: ReturnType<typeof setTimeout> | null = null;
	let holdFired = false;
	let pointerType: 'mouse' | 'touch' = 'mouse';
	let hoverPillIdx = $state(-1);

	let scopeOptions = $derived([
		{ id: null as string | null, name: 'Workspace', color: 'var(--violet-brand)' },
		...activeProjects
			.slice()
			.sort((a, b) => a.name.localeCompare(b.name))
			.map((p) => ({ id: p.id, name: p.name, color: projectAccent(p.id, p.accent_color) }))
	]);

	function clearHoldTimer() {
		if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
	}

	function closeScopePills() {
		scopePillsOpen = false;
		hoverPillIdx = -1;
		clearHoldTimer();
	}

	function pickScope(opt: { id: string | null; name: string; color: string }) {
		closeScopePills();
		if (opt.id == null) exitScope();
		else enterScope({ id: opt.id, name: opt.name, color: opt.color });
		onNewChat?.();
	}

	function newChatHere() {
		closeScopePills();
		onNewChat?.();
	}

	function fabPressStart(e: PointerEvent) {
		if (e.button !== 0 && e.pointerType === 'mouse') return;
		pointerType = e.pointerType === 'touch' ? 'touch' : 'mouse';
		holdFired = false;
		hoverPillIdx = -1;
		clearHoldTimer();
		holdTimer = setTimeout(() => {
			holdFired = true;
			scopePillsOpen = true;
		}, HOLD_MS);
	}

	function fabPressEnd() {
		clearHoldTimer();
		if (!holdFired) {
			newChatHere();
			return;
		}
		if (pointerType === 'touch' && hoverPillIdx >= 0 && hoverPillIdx < scopeOptions.length) {
			pickScope(scopeOptions[hoverPillIdx]);
		}
	}

	function fabPressCancel() { closeScopePills(); }

	function onFabTouchMove(e: TouchEvent) {
		if (!scopePillsOpen || pointerType !== 'touch') return;
		const t = e.touches[0];
		const el = document.elementFromPoint(t.clientX, t.clientY) as HTMLElement | null;
		const pill = el?.closest('[data-pill-idx]') as HTMLElement | null;
		hoverPillIdx = pill ? Number(pill.dataset.pillIdx) : -1;
	}

	function onPillKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') { closeScopePills(); }
	}

	function fabClick(e: MouseEvent) {
		if (e.detail !== 0) return;
		newChatHere();
	}
</script>

<div
	class="rail-newchat-wrap"
	class:collapsed={$sidebarCollapsed}
	class:armed={scopePillsOpen}
	class:pane-open={$paneOpen || $paneDragging}
	class:sidebar-closed={!sidebarOpen}
>
	{#if scopePillsOpen}
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="scope-pills-backdrop" onclick={closeScopePills}></div>
		<div class="scope-pills" role="menu" aria-label="Create chat in scope">
			<div class="scope-pills-cap">New chat in…</div>
			{#each scopeOptions as opt, i (opt.id ?? 'workspace')}
				<button
					class="scope-pill press-tint"
					class:hover={hoverPillIdx === i}
					class:current={(opt.id ?? null) === ($uiScope?.id ?? null)}
					data-pill-idx={i}
					role="menuitem"
					in:fly={{ y: 8, duration: 220, delay: i * 22, easing: backOut }}
					onmouseenter={() => (hoverPillIdx = i)}
					onmouseleave={() => (hoverPillIdx = -1)}
					onclick={() => pickScope(opt)}
				>
					<span class="scope-pill-dot" style="background: {opt.color};"></span>
					<span class="scope-pill-name">{opt.name}</span>
					{#if (opt.id ?? null) === ($uiScope?.id ?? null)}
						<span class="scope-pill-here">here</span>
					{/if}
				</button>
			{/each}
		</div>
	{/if}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<button
		class="rail-newchat"
		class:armed={scopePillsOpen}
		title="New chat — hold to pick a scope"
		aria-label="New chat"
		aria-haspopup="menu"
		aria-expanded={scopePillsOpen}
		onpointerdown={fabPressStart}
		onpointerup={fabPressEnd}
		onpointerleave={() => { if (pointerType === 'mouse') clearHoldTimer(); }}
		onpointercancel={fabPressCancel}
		oncontextmenu={(e: MouseEvent) => e.preventDefault()}
		ontouchmove={onFabTouchMove}
		onclick={fabClick}
		onkeydown={onPillKeydown}
	>
		<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
			<path d="M12 20h9" />
			<path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5Z" />
		</svg>
	</button>
</div>

<style>
	.rail-newchat-wrap {
		position: absolute;
		left: calc(var(--sidebar-width) - 64px);
		bottom: 16px;
		width: 48px; height: 48px;
		z-index: 2;
	}
	.rail-newchat-wrap.armed { z-index: 60; }
	.rail-newchat-wrap.collapsed { display: none; }
	@media (max-width: 1024px) {
		.rail-newchat-wrap.pane-open { display: none; }
	}
	.rail-newchat {
		position: relative;
		width: 48px; height: 48px; border-radius: var(--radius-circle);
		display: flex; align-items: center; justify-content: center;
		background: var(--accent); color: var(--on-accent); border: none; cursor: pointer;
		/* The one elevation its rail peers (`MobileScrollFab`, `PlanDrawerControl`)
		   already carry. Feedback stays in the transforms below, so elevation never
		   forks into a per-component ladder. */
		box-shadow: var(--glass-elevation-1), var(--glass-rim);
		-webkit-touch-callout: none; touch-action: none; user-select: none; -webkit-user-select: none;
		transition: transform var(--dur-tight) var(--ease-spring), background var(--dur-tight) ease;
	}
	.rail-newchat:hover { transform: translateY(-1px); }
	.rail-newchat:active { scale: 0.9; }
	.rail-newchat.armed { transform: scale(1.04); }

	.scope-pills-backdrop { position: fixed; inset: 0; z-index: 9; background: transparent; border: none; }
	.scope-pills {
		position: absolute; z-index: 10;
		right: 0; bottom: 56px;
		display: flex; flex-direction: column-reverse; align-items: stretch; gap: 6px;
		min-width: 184px; max-width: 240px;
		max-height: min(60vh, 440px); overflow-y: auto;
		padding: 6px;
		background: var(--glass-regular-thick, var(--bg-secondary));
		backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
		border: 1px solid var(--border); border-radius: var(--lg-card-radius);
		box-shadow: var(--glass-rim), var(--shadow-chip, 0 8px 32px rgba(0,0,0,0.4));
	}
	.scope-pills-cap {
		order: 1;
		font-size: var(--text-caption); font-weight: var(--fw-bold); text-transform: uppercase;
		letter-spacing: var(--ls-cap); color: var(--text-tertiary);
		padding: 4px 8px 2px; pointer-events: none;
	}
	.scope-pill {
		display: flex; align-items: center; gap: 9px;
		padding: 8px 12px; border-radius: var(--radius-sm);
		background: none; border: none; cursor: pointer; text-align: left;
		color: var(--text-primary); font-size: var(--text-subhead);
		transition: background var(--dur-tight) ease;
	}
	.scope-pill:hover, .scope-pill.hover { background: var(--bg-hover); }
	.scope-pill.current { background: var(--violet-bg); }
	.scope-pill-dot {
		width: 9px; height: 9px; border-radius: var(--radius-circle); flex-shrink: 0;
		box-shadow: 0 0 0 1px color-mix(in srgb, var(--text-primary) 8%, transparent);
	}
	.scope-pill-name {
		flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
	}
	.scope-pill-here {
		flex-shrink: 0; font-size: var(--text-caption); font-weight: var(--fw-bold);
		text-transform: uppercase; letter-spacing: var(--ls-cap);
		color: var(--violet-text); background: color-mix(in srgb, var(--accent) 14%, transparent);
		padding: 1px 6px; border-radius: var(--radius-pill);
	}

	@media (max-width: 768px) {
		.rail-newchat-wrap.sidebar-closed { display: none; }
		.rail-newchat-wrap {
			bottom: calc(var(--safe-bottom, 0px) + 16px);
		}
	}
</style>
