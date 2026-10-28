<script lang="ts">
	import { threads, activeThread, activeThreadId, isNavAgent, saveThreadTitle } from '$lib/stores/threads';
	import { canvasHeader } from '$lib/stores/canvasHeader';
	import { threadActivity, descendantRunning } from '$lib/stores/activity';
	import { token } from '$lib/stores/auth';
	import { uiScope } from '$lib/stores/uiScope';
	import { haptic } from '$lib/haptic';
	import { toasts } from '$lib/stores/toast';
	import { threadPlanStates } from '$lib/stores/threadPlanStates';
	import { COLLAPSED_AGENT_LIMIT, selectAgentRows } from './agentVisibility';
	import ThreadPlanStateMark from './ThreadPlanStateMark.svelte';
	import ThreadPausedMark from './ThreadPausedMark.svelte';
	import ThreadWaitingMark from './ThreadWaitingMark.svelte';
	import { showsWaitingGlyph, waitMarkTitle } from './threadRowMark';
	import { minuteClock } from '$lib/time';
	import type { View } from '$lib/homePaneConfig';

	type MenuKind = 'agent' | 'conductor';

	let {
		onConductor,
		onSelectThread,
		onOpenAttention,
		onOpenContextMenu,
		conductorThreadId = null,
		conductorKnownAbsent = false,
		activeView = 'chat',
	}: {
		onConductor?: () => boolean | void | Promise<boolean | void>;
		onSelectThread?: (id: string) => void | Promise<void>;
		onOpenAttention?: (threadId: string, title: string) => void;
		onOpenContextMenu?: (event: MouseEvent, id: string, kind: MenuKind) => void;
		conductorThreadId?: string | null;
		conductorKnownAbsent?: boolean;
		activeView?: View;
	} = $props();

	let expanded = $state(false);
	let drawerScope = $state('');
	let renamingThreadId = $state<string | null>(null);
	let renameValue = $state('');
	let holdTimer: ReturnType<typeof setTimeout> | null = null;
	let holdId: string | null = null;
	let holdX = 0;
	let holdY = 0;
	let suppressClickId: string | null = null;


	let conductorActive = $derived(
		activeView === 'chat' && $canvasHeader == null && $activeThread == null,
	);
	// An unresolved lookup stays clickable: a failed fetch must not read as
	// "no conductor" — only the API's explicit absent answer disables the entry.
	// The workspace scope's occupant is the CORSIA agent; a project's is its conductor.
	let entryLabel = $derived($uiScope ? 'Conductor' : 'CORSIA');
	let conductorClickable = $derived(conductorThreadId !== null || !conductorKnownAbsent);
	let conductorState = $derived(conductorThreadId ? $threadActivity[conductorThreadId] : undefined);
	let conductorBusy = $derived(conductorState === 'active');
	let conductorSummary = $derived(conductorThreadId ? $threadPlanStates[conductorThreadId]?.latest_update?.summary : undefined);
	let scopedAgents = $derived(
		$threads.filter((thread) => (
			isNavAgent(thread) && ($uiScope ? thread.project_id === $uiScope.id : !thread.project_id)
		)),
	);
	let selection = $derived(
		selectAgentRows(scopedAgents, $threadActivity, $threadPlanStates, $activeThreadId, expanded),
	);
	let hasOverflow = $derived(scopedAgents.length > COLLAPSED_AGENT_LIMIT);
	let hiddenAttention = $derived([
		selection.hiddenWorking ? `${selection.hiddenWorking} working` : '',
		selection.hiddenUnread ? `${selection.hiddenUnread} unread` : '',
	].filter(Boolean).join(' · '));

	$effect(() => {
		const nextScope = $uiScope?.id ?? 'workspace';
		if (nextScope === drawerScope) return;
		drawerScope = nextScope;
		expanded = false;
		renamingThreadId = null;
	});

	function clearHold(): void {
		if (holdTimer) clearTimeout(holdTimer);
		holdTimer = null;
		holdId = null;
	}

	function startHold(event: PointerEvent, id: string, kind: MenuKind): void {
		suppressClickId = null;
		if (event.pointerType === 'mouse' || !onOpenContextMenu) return;
		clearHold();
		holdId = id;
		holdX = event.clientX;
		holdY = event.clientY;
		holdTimer = setTimeout(() => {
			suppressClickId = id;
			haptic();
			onOpenContextMenu(
				new MouseEvent('contextmenu', { clientX: holdX, clientY: holdY }),
				id,
				kind,
			);
			clearHold();
		}, 500);
	}

	function moveHold(event: PointerEvent): void {
		if (!holdId) return;
		if (Math.hypot(event.clientX - holdX, event.clientY - holdY) > 8) clearHold();
	}

	function openMenu(event: MouseEvent, id: string | null, kind: MenuKind): void {
		if (!id || !onOpenContextMenu) return;
		clearHold();
		onOpenContextMenu(event, id, kind);
	}

	function consumeSuppressedClick(id: string): boolean {
		if (suppressClickId !== id) return false;
		suppressClickId = null;
		return true;
	}

	async function goConductor(): Promise<boolean> {
		if (conductorKnownAbsent) return false;
		if (conductorThreadId && consumeSuppressedClick(conductorThreadId)) return false;
		if (!onConductor) return false;
		return await onConductor() !== false;
	}

	async function selectAgent(id: string): Promise<boolean> {
		if (consumeSuppressedClick(id) || renamingThreadId === id) return false;
		if (!onSelectThread) return false;
		await onSelectThread(id);
		return true;
	}

	function onAgentKeydown(event: KeyboardEvent, id: string): void {
		if (event.key !== 'Enter' && event.key !== ' ') return;
		event.preventDefault();
		selectAgent(id);
	}

	export function startRename(id: string): void {
		const agent = scopedAgents.find((thread) => thread.id === id);
		if (!agent) return;
		renamingThreadId = id;
		renameValue = agent.title;
	}

	async function finishRename(): Promise<void> {
		const id = renamingThreadId;
		const title = renameValue.trim();
		renamingThreadId = null;
		if (!id || !title || !$token) return;
		try {
			await saveThreadTitle(id, title, $token);
		} catch {
			toasts.show("Couldn't rename Agent", 'error');
		}
	}
</script>

<nav class="scoped" aria-label="Agents">
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="scoped-item identity conductor press-tint"
		class:active={conductorActive && conductorClickable}
		class:disabled={!conductorClickable}
		onclick={goConductor}
		oncontextmenu={(event) => openMenu(event, conductorThreadId, 'conductor')}
		onpointerdown={(event) => conductorThreadId && startHold(event, conductorThreadId, 'conductor')}
		onpointermove={moveHold}
		onpointerup={clearHold}
		onpointercancel={clearHold}
		title={conductorThreadId
			? `${entryLabel} — main chat for this ${$uiScope ? 'project' : 'workspace'}`
			: conductorKnownAbsent
				? `No main chat for this ${$uiScope ? 'project' : 'workspace'}`
				: 'Open main chat'}
	>
		<button
			type="button"
			class="scoped-item-trigger"
			disabled={!conductorClickable}
			aria-label={`${entryLabel}${conductorSummary ? `. Latest update: ${conductorSummary}` : ''}`}
		>
			{#if conductorThreadId && $threadPlanStates[conductorThreadId]?.paused}
				<ThreadPausedMark />
			{:else}
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l1.8 5 5 1.8-5 1.8L12 17l-1.8-5-5-1.8 5-1.8z"/></svg>
			{/if}
			<div class="agent-copy">
				<span class="lbl">{entryLabel}</span>
				{#if conductorSummary}
					<span class="update-summary" title={conductorSummary}>{conductorSummary}</span>
				{/if}
			</div>
		</button>
		{#if conductorThreadId}
			<ThreadPlanStateMark threadId={conductorThreadId} {onOpenAttention} onOpenThread={goConductor} />
		{/if}
		{#if conductorBusy}
			<svg class="spinner" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--status-in-progress)" stroke-width="3" role="img" aria-label="Working"><title>Working</title><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
		{:else if conductorThreadId && $descendantRunning.has(conductorThreadId)}
			<svg class="spinner" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--text-tertiary)" stroke-width="3" role="img" aria-label="Subagent working"><title>Subagent working</title><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
		{:else if conductorThreadId && showsWaitingGlyph($threadPlanStates[conductorThreadId], false)}
			<span class="waiting-slot"><ThreadWaitingMark size={12} title={waitMarkTitle($threadPlanStates[conductorThreadId], $minuteClock)} /></span>
		{/if}
	</div>

	{#each selection.visible as agent (agent.id)}
		{@const activity = $threadActivity[agent.id]}
		{@const summary = $threadPlanStates[agent.id]?.latest_update?.summary}
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div
			class="scoped-item identity agent press-tint"
			class:active={$activeThreadId === agent.id}
			onclick={() => selectAgent(agent.id)}
			oncontextmenu={(event) => openMenu(event, agent.id, 'agent')}
			onpointerdown={(event) => startHold(event, agent.id, 'agent')}
			onpointermove={moveHold}
			onpointerup={clearHold}
			onpointercancel={clearHold}
			title={agent.agent_responsibility ?? agent.title}
		>
			<div
				class="scoped-item-trigger"
				role="button"
				tabindex="0"
				onkeydown={(event) => onAgentKeydown(event, agent.id)}
				aria-label={`Agent: ${agent.title}${summary ? `. Latest update: ${summary}` : ''}`}
			>
				{#if $threadPlanStates[agent.id]?.paused}
					<ThreadPausedMark />
				{:else}
					<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l1.8 5 5 1.8-5 1.8L12 17l-1.8-5-5-1.8 5-1.8z"/></svg>
				{/if}
				{#if renamingThreadId === agent.id}
					<!-- svelte-ignore a11y_autofocus -->
					<input
						class="rename-input"
						bind:value={renameValue}
						onclick={(event) => event.stopPropagation()}
						onblur={finishRename}
						onkeydown={(event) => {
							event.stopPropagation();
							if (event.key === 'Enter') {
								event.preventDefault();
								finishRename();
							}
							if (event.key === 'Escape') {
								event.preventDefault();
								renamingThreadId = null;
							}
						}}
						aria-label="Rename Agent"
						autofocus
					/>
				{:else}
					<div class="agent-copy">
						<span class="lbl">{agent.title}</span>
						{#if summary}
							<span class="update-summary" title={summary}>{summary}</span>
						{/if}
					</div>
				{/if}
			</div>
			<ThreadPlanStateMark threadId={agent.id} {onOpenAttention} onOpenThread={selectAgent} />
			{#if activity === 'active'}
				<svg class="spinner" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--status-in-progress)" stroke-width="3" role="img" aria-label="Working"><title>Working</title><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
			{:else if $descendantRunning.has(agent.id)}
				<svg class="spinner" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--text-tertiary)" stroke-width="3" role="img" aria-label="Subagent working"><title>Subagent working</title><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
			{:else if showsWaitingGlyph($threadPlanStates[agent.id], false)}
				<span class="waiting-slot"><ThreadWaitingMark size={12} title={waitMarkTitle($threadPlanStates[agent.id], $minuteClock)} /></span>
			{/if}
		</div>
	{/each}

	{#if hasOverflow}
		<button
			class="drawer-toggle press-tint"
			aria-expanded={expanded}
			onclick={() => (expanded = !expanded)}
		>
			<svg class:expanded width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18l6-6-6-6"/></svg>
			<span class="drawer-label">{expanded ? 'Show less' : `Show ${selection.hidden.length} more`}</span>
			{#if !expanded && hiddenAttention}
				<span class="drawer-separator" aria-hidden="true">·</span>
				<span class="drawer-attention">{hiddenAttention}</span>
			{/if}
		</button>
	{/if}
</nav>

<style>
	.scoped {
		display: flex; flex-direction: column; gap: 1px;
		padding: 6px 8px 8px;
		margin: 0 0 2px;
	}
	.scoped-item {
		display: flex; align-items: center; gap: 10px; width: 100%; text-align: left;
		padding: 7px 10px; border-radius: var(--radius-sm, 8px);
		background: none; border: none; cursor: pointer;
		font-family: inherit; font-size: var(--text-subhead); font-weight: 400;
		color: var(--text-secondary); position: relative;
		transition: background var(--dur-tight) ease, color var(--dur-tight) ease;
		-webkit-touch-callout: none; user-select: none;
	}
	.scoped-item:hover { background: var(--bg-hover); color: var(--text-primary); }
	.scoped-item.disabled { cursor: default; color: var(--text-tertiary); }
	.scoped-item.disabled:hover { background: none; color: var(--text-tertiary); }
	.scoped-item svg { flex-shrink: 0; }
	.scoped-item-trigger {
		display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0;
		background: none; border: none; padding: 0; margin: 0;
		font: inherit; font-size: inherit; color: inherit; text-align: left; cursor: pointer;
	}
	.scoped-item-trigger:disabled { cursor: default; }
	.scoped-item.identity .scoped-item-trigger > svg:first-child { color: var(--accent-text); opacity: 1; }
	.agent-copy { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
	.lbl, .update-summary { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.lbl { font-size: var(--text-subhead); }
	.update-summary { color: var(--text-tertiary); font-size: var(--text-caption); line-height: 1.2; }
	.rename-input {
		flex: 1; min-width: 0; padding: 2px 6px;
		font: inherit; color: var(--text-primary);
		background: var(--bg-tertiary); border: 1px solid var(--accent);
		border-radius: var(--radius-2xs); outline: none;
	}
	.spinner { flex-shrink: 0; animation: spin 0.9s linear infinite; }
	.waiting-slot { display: inline-flex; flex-shrink: 0; color: var(--text-tertiary); }
	@media (prefers-reduced-motion: reduce) {
		/* The arc stays, still titled, so reduced motion reads the same truth. */
		.spinner { animation: none; }
	}

	.scoped-item.conductor .lbl { font-weight: 600; color: var(--text-primary); }
	.scoped-item.conductor.disabled .lbl { color: var(--text-tertiary); }
	.scoped-item.conductor.disabled svg { color: var(--text-tertiary); opacity: 0.8; }
	.scoped-item.conductor svg { color: var(--accent-text); opacity: 1; }
	.scoped-item.active { background: var(--accent-wash); }
	.drawer-toggle {
		display: flex; align-items: center; gap: 6px; width: 100%; min-width: 0;
		padding: 7px 10px; border: none; border-radius: var(--radius-sm);
		background: none; color: var(--text-tertiary); cursor: pointer;
		font-family: inherit; font-size: var(--text-footnote); text-align: left;
		transition: background var(--dur-tight), color var(--dur-tight);
	}
	.drawer-toggle:hover { background: var(--bg-hover); color: var(--text-secondary); }
	.drawer-toggle svg { flex-shrink: 0; transition: transform var(--dur-tight) ease; }
	.drawer-toggle svg.expanded { transform: rotate(90deg); }
	.drawer-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.drawer-separator { flex-shrink: 0; color: var(--text-tertiary); }
	.drawer-attention {
		flex-shrink: 0; max-width: 52%; overflow: hidden; text-overflow: ellipsis;
		white-space: nowrap; color: var(--accent-text); font-weight: 600;
	}
	@keyframes spin { to { transform: rotate(360deg); } }

	@media (max-width: 768px) {
		.scoped-item { min-height: 44px; padding: 10px; }
		.drawer-toggle { min-height: 40px; padding: 9px 10px; }
	}
</style>
