<script lang="ts">
	import { pinnedConversations } from '$lib/stores/pinnedConversations';
	import { projectAccent } from '$lib/utils/projectColors';

	let {
		threadId,
		kind = 'conversation',
		isUnread = false,
		canMarkSeen = false,
		x,
		y,
		projects = [],
		currentProjectId = null,
		onToggleUnread,
		onPin,
		onRename,
		onAgentSettings,
		onMoveToProject,
		onArchive,
		onDeactivateAgent,
		onDelete,
		onClose,
	}: {
		threadId: string;
		kind?: 'conversation' | 'agent' | 'conductor';
		isUnread?: boolean;
		/** Only a thread the user has opened has seen state to mark; never fabricate a visit. */
		canMarkSeen?: boolean;
		x: number;
		y: number;
		projects?: { id: string; name: string; accent_color?: string | null }[];
		currentProjectId?: string | null;
		onToggleUnread: () => void;
		onPin: () => void;
		onRename: () => void;
		onAgentSettings: () => void;
		onMoveToProject: (projectId: string | null) => void;
		onArchive: () => void;
		onDeactivateAgent: () => void;
		onDelete: () => void;
		onClose: () => void;
	} = $props();

	const isPinned = $derived($pinnedConversations.has(threadId));

	const projectEntries = $derived([...projects].sort((a, b) => a.name.localeCompare(b.name)));

	let submenuOpen = $state(false);
	let subAnchorEl = $state<HTMLButtonElement | undefined>(undefined);
	let subAnchorRect = $state<{ top: number; right: number; left: number } | null>(null);
	const MENU_W = 188;
	const SUB_W = 184;
	const MOBILE_BREAKPOINT = 768;
	const estimatedHeight = $derived(kind === 'conversation' ? 304 : kind === 'agent' ? 196 : 52);
	const clampedLeft = $derived(Math.max(8, Math.min(x, window.innerWidth - MENU_W - 8)));
	const clampedTop = $derived(Math.max(8, Math.min(y, window.innerHeight - estimatedHeight - 8)));

	// The submenu renders as an independent fixed-position sibling of
	// .context-menu rather than a descendant. An absolutely-positioned
	// child sitting outside its ancestor's box still counts toward that
	// ancestor's scrollable region if the ancestor has overflow set —
	// that's what produced the double vertical+horizontal scrollbar bug.
	const submenuPos = $derived.by(() => {
		if (!subAnchorRect) return { top: 0, left: 0, maxHeight: 300 };
		if (window.innerWidth <= MOBILE_BREAKPOINT) {
			const maxHeight = Math.min(window.innerHeight - 24, 400);
			return { top: window.innerHeight - maxHeight - 12, left: 12, maxHeight };
		}
		const flip = subAnchorRect.right + 3 + SUB_W > window.innerWidth;
		const left = Math.max(8, flip ? subAnchorRect.left - SUB_W - 3 : subAnchorRect.right + 3);
		const top = Math.max(8, subAnchorRect.top - 5);
		return { top, left, maxHeight: window.innerHeight - top - 24 };
	});

	let closeTimer: ReturnType<typeof setTimeout> | null = null;
	function cancelClose() { if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; } }
	function openSub() {
		cancelClose();
		if (subAnchorEl) subAnchorRect = subAnchorEl.getBoundingClientRect();
		submenuOpen = true;
	}
	function scheduleClose() { cancelClose(); closeTimer = setTimeout(() => { submenuOpen = false; closeTimer = null; }, 140); }
	function closeSubNow() { cancelClose(); submenuOpen = false; }

	function pick(projectId: string | null) {
		onMoveToProject(projectId);
	}
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="context-menu-backdrop" onclick={onClose}></div>
<div class="context-menu" style="left: {clampedLeft}px; top: {clampedTop}px;">
	{#if canMarkSeen}
		<button class="press-tint context-menu-item" onclick={onToggleUnread} onmouseenter={closeSubNow}>
			<svg width="14" height="14" viewBox="0 0 24 24" fill={isUnread ? 'currentColor' : 'none'} stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8"/></svg>
			{isUnread ? 'Mark read' : 'Mark unread'}
		</button>
	{/if}
	{#if kind === 'conversation'}
		<button class="press-tint context-menu-item" onclick={onPin} onmouseenter={closeSubNow}>
			<svg width="14" height="14" viewBox="0 0 24 24" fill={isPinned ? 'currentColor' : 'none'} stroke="currentColor" stroke-width="2"><path d="M16 3a1 1 0 00-1 1v1.586l-6.293 6.293A1 1 0 008 12.586V15H5a1 1 0 000 2h2v2a1 1 0 002 0v-2h2.414a1 1 0 00.707-.293L18.414 11H20a1 1 0 001-1V4a1 1 0 00-1-1h-4z"/></svg>
			{isPinned ? 'Unpin' : 'Pin to top'}
		</button>
	{/if}
	{#if kind !== 'conductor'}
		<button class="press-tint context-menu-item" onclick={onRename} onmouseenter={closeSubNow}>
			<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
			Rename
		</button>
	{/if}

	{#if kind === 'conversation'}
		<div class="submenu-anchor">
			<button
				bind:this={subAnchorEl}
				class="press-tint context-menu-item has-sub"
				class:active={submenuOpen}
				aria-haspopup="menu"
				aria-expanded={submenuOpen}
				onmouseenter={openSub}
				onmouseleave={scheduleClose}
				onclick={() => (submenuOpen ? closeSubNow() : openSub())}
			>
				<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/></svg>
				<span class="item-label">Move to project</span>
				<svg class="sub-caret" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>
			</button>
		</div>

		<button class="press-tint context-menu-item" onclick={onArchive} onmouseenter={closeSubNow}>
			<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5"/><line x1="10" y1="12" x2="14" y2="12"/></svg>
			Archive
		</button>
		<div class="context-menu-divider"></div>
		<button class="press-tint context-menu-item danger" onclick={onDelete} onmouseenter={closeSubNow}>
			<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
			Delete
		</button>
	{:else if kind === 'agent'}
		<button class="press-tint context-menu-item" onclick={onAgentSettings} onmouseenter={closeSubNow}>
			<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.12 2.12-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1 1.55V20h-3v-.09a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.88.34l-.06.06-2.12-2.12.06-.06A1.7 1.7 0 0 0 7.1 15a1.7 1.7 0 0 0-1.55-1H5v-3h.55A1.7 1.7 0 0 0 7.1 10a1.7 1.7 0 0 0-.34-1.88l-.06-.06L8.82 5.94l.06.06a1.7 1.7 0 0 0 1.88.34 1.7 1.7 0 0 0 1-1.55V4h3v.79a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.88-.34l.06-.06 2.12 2.12-.06.06A1.7 1.7 0 0 0 19.4 10a1.7 1.7 0 0 0 1.55 1h.05v3h-.05a1.7 1.7 0 0 0-1.55 1Z"/></svg>
			Agent settings
		</button>
		<div class="context-menu-divider"></div>
		<button class="press-tint context-menu-item deactivate" onclick={onDeactivateAgent} onmouseenter={closeSubNow}>
			<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M12 5v14" transform="rotate(45 12 12)"/></svg>
			Deactivate Agent
		</button>
	{/if}
</div>

{#if submenuOpen}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="submenu"
		role="menu"
		tabindex="-1"
		style="top: {submenuPos.top}px; left: {submenuPos.left}px; max-height: {submenuPos.maxHeight}px;"
		onmouseenter={openSub}
		onmouseleave={scheduleClose}
	>
		<button
			class="press-tint context-menu-item submenu-item"
			class:selected={currentProjectId == null}
			role="menuitemradio"
			aria-checked={currentProjectId == null}
			onclick={() => pick(null)}
		>
			<span class="proj-dot none"></span>
			<span class="item-label">No project</span>
			{#if currentProjectId == null}
				<svg class="check" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"/></svg>
			{/if}
		</button>
		{#if projectEntries.length > 0}
			<div class="submenu-divider"></div>
			{#each projectEntries as p (p.id)}
				{@const selected = currentProjectId === p.id}
				<button
					class="press-tint context-menu-item submenu-item"
					class:selected
					role="menuitemradio"
					aria-checked={selected}
					onclick={() => pick(p.id)}
				>
					<span class="proj-dot" style="background: {projectAccent(p.id, p.accent_color)};"></span>
					<span class="item-label">{p.name}</span>
					{#if selected}
						<svg class="check" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"/></svg>
					{/if}
				</button>
			{/each}
		{:else}
			<div class="submenu-empty">No projects yet</div>
		{/if}
	</div>
{/if}

<style>
	.context-menu-backdrop {
		position: fixed; inset: 0; z-index: 200;
	}
	.context-menu {
		position: fixed; z-index: 201;
		background: var(--bg-elevated);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		box-shadow: 0 4px 16px rgba(0,0,0,0.2);
		padding: 4px;
		min-width: 188px;
		max-height: calc(100vh - 16px);
		overflow-x: hidden;
		overflow-y: auto;
		transform: translateY(-4px);
	}
	.context-menu-item {
		display: flex; align-items: center; gap: 8px;
		width: 100%; padding: 7px 10px;
		background: none; border: none; border-radius: var(--radius-2xs);
		color: var(--text-primary); font-family: inherit; font-size: var(--text-subhead);
		cursor: pointer; text-align: left;
		transition: background var(--dur-tight);
	}
	.context-menu-item:hover { background: var(--bg-hover); }
	.context-menu-item.danger { color: var(--red); }
	.context-menu-item.danger:hover { background: rgba(239, 68, 68, 0.1); }
	.context-menu-item.deactivate { color: var(--amber); }
	.context-menu-item.deactivate:hover { background: color-mix(in srgb, var(--amber) 12%, transparent); }
	.context-menu-divider {
		height: 1px; background: var(--border-subtle); margin: 4px 0;
	}

	.context-menu-item.has-sub .item-label { flex: 1; }
	.context-menu-item.has-sub .sub-caret { color: var(--text-tertiary); flex-shrink: 0; }
	.context-menu-item.has-sub.active { background: var(--bg-hover); }
	.context-menu-item.has-sub.active .sub-caret { color: var(--text-secondary); }

	.submenu {
		position: fixed;
		z-index: 202;
		min-width: 184px; max-width: 184px;
		background: var(--bg-elevated);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		box-shadow: 0 4px 16px rgba(0,0,0,0.2);
		padding: 4px;
		overflow-x: hidden;
		overflow-y: auto;
	}
	.submenu-item .item-label {
		flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
	}
	.submenu-item.selected { color: var(--accent); }
	.submenu-item .check { color: var(--accent); flex-shrink: 0; }
	.submenu-divider { height: 1px; background: var(--border-subtle); margin: 4px 2px; }
	.submenu-empty {
		padding: 8px 10px; font-size: var(--text-footnote); color: var(--text-tertiary); text-align: center;
	}
	.proj-dot {
		width: 8px; height: 8px; border-radius: var(--radius-circle); flex-shrink: 0;
		box-shadow: 0 0 0 1px color-mix(in srgb, var(--text-primary) 8%, transparent);
	}
	.proj-dot.none {
		background: transparent;
		box-shadow: inset 0 0 0 1.5px var(--text-tertiary);
	}

	@media (max-width: 768px) {
		.context-menu-item {
			padding: 12px 10px;
		}
		.submenu {
			min-width: 200px; max-width: 86vw;
		}
		.submenu-item { padding: 12px 10px; }
	}
</style>
