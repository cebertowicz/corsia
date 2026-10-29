<script lang="ts">
	import { fade, fly } from 'svelte/transition';
	import { backOut } from 'svelte/easing';
	import { tick } from 'svelte';
	import { paletteOpen } from '$lib/stores/palette';
	import { everyThread } from '$lib/stores/threads';
	import { projects as projectsStore, loadProjects, type ProjectPick } from '$lib/stores/projects';
	import { createProjectOpen } from '$lib/stores/overlays';
	import { projectAccent } from '$lib/utils/projectColors';

	let { onPickProject, onPickConversation, onNewChat, onGoWorkspace }: {
		onPickProject: (p: ProjectPick) => void;
		onPickConversation: (id: string) => void;
		onNewChat: () => void;
		onGoWorkspace: () => void;
	} = $props();

	let inputEl = $state<HTMLInputElement | undefined>(undefined);
	let query = $state('');

	$effect(() => {
		if ($paletteOpen) {
			query = '';
			sel = 0;
			loadProjects();
			tick().then(() => inputEl?.focus());
		}
	});

	const q = $derived(query.trim().toLowerCase());
	const activeProjects = $derived($projectsStore.filter((p) => p.status === 'active'));
	const filteredProjects = $derived(
		q ? activeProjects.filter((p) => p.name.toLowerCase().includes(q)) : activeProjects
	);
	const filteredConversations = $derived(
		[...$everyThread]
			.sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())
			.filter((s) => !q || (s.title ?? '').toLowerCase().includes(q))
			.slice(0, q ? 25 : 8)
	);
	const projectName = $derived(Object.fromEntries($projectsStore.map((p) => [p.id, p.name])));

	function close() { paletteOpen.set(false); }
	function pickProject(p: ProjectPick) { close(); onPickProject(p); }
	function pickConversation(id: string) { close(); onPickConversation(id); }
	function newChat() { close(); onNewChat(); }
	function goWorkspace() { close(); onGoWorkspace(); }
	function newProject() { close(); createProjectOpen.set(true); }

	type Action = { id: string; label: string; run: () => void };
	const actions: Action[] = [
		{ id: 'new-chat', label: 'New chat', run: newChat },
		{ id: 'go-workspace', label: 'Go to workspace', run: goWorkspace },
		{ id: 'new-project', label: 'New project', run: newProject },
	];
	const filteredActions = $derived(actions.filter((action) => !q || action.label.toLowerCase().includes(q)));

	let sel = $state(0);
	const items = $derived([
		...filteredActions,
		...filteredProjects.map((p) => ({ run: () => pickProject(p) })),
		...filteredConversations.map((c) => ({ run: () => pickConversation(c.id) })),
	]);
	const actionIndex = (id: string) => filteredActions.findIndex((action) => action.id === id);
	const projBase = $derived(filteredActions.length);
	const convBase = $derived(projBase + filteredProjects.length);

	function resetSel() { sel = 0; }
	$effect(() => {
		const visibleItems = items;
		sel = visibleItems.length === 0 ? 0 : Math.min(sel, visibleItems.length - 1);
	});
	$effect(() => {
		sel;
		if (!$paletteOpen) return;
		tick().then(() => document.querySelector('.palette .res.sel')?.scrollIntoView({ block: 'nearest' }));
	});

	function onKey(e: KeyboardEvent) {
		if (!$paletteOpen) return;
		if (e.key === 'Escape') close();
		else if (e.key === 'ArrowDown' && items.length > 0) { e.preventDefault(); sel = Math.min(sel + 1, items.length - 1); }
		else if (e.key === 'ArrowUp' && items.length > 0) { e.preventDefault(); sel = Math.max(sel - 1, 0); }
		else if (e.key === 'Enter' && items.length > 0) { e.preventDefault(); items[sel]?.run(); }
	}
</script>

<svelte:window onkeydown={onKey} />

{#if $paletteOpen}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="backdrop" transition:fade={{ duration: 120 }} onclick={close}></div>
	<div class="palette" transition:fly={{ y: -10, duration: 380, easing: backOut }}>
		<div class="search">
			<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
			<input bind:this={inputEl} bind:value={query} oninput={resetSel} placeholder="Search or run an action…" />
			<button class="pal-close" onclick={close} aria-label="Close search">
				<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 5l14 14M19 5L5 19"/></svg>
			</button>
		</div>
		<div class="results">
			{#if filteredActions.length > 0}
				<div class="g-label">Actions</div>
			{/if}
			{#if actionIndex('new-chat') >= 0}
				<button class="res" class:sel={sel === actionIndex('new-chat')} onmouseenter={() => (sel = actionIndex('new-chat'))} onclick={newChat}>
				<span class="res-icon">
					<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5Z"/></svg>
				</span>
				<span class="res-label">New chat</span>
				</button>
			{/if}
			{#if actionIndex('go-workspace') >= 0}
				<button class="res" class:sel={sel === actionIndex('go-workspace')} onmouseenter={() => (sel = actionIndex('go-workspace'))} onclick={goWorkspace}>
				<span class="res-icon">
					<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/></svg>
				</span>
				<span class="res-label">Go to workspace</span>
				</button>
			{/if}
			{#if actionIndex('new-project') >= 0}
				<button class="res" class:sel={sel === actionIndex('new-project')} onmouseenter={() => (sel = actionIndex('new-project'))} onclick={newProject}>
				<span class="res-icon">
					<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/><line x1="12" y1="10" x2="12" y2="16"/><line x1="9" y1="13" x2="15" y2="13"/></svg>
				</span>
				<span class="res-label">New project</span>
				</button>
			{/if}

			<div class="g-label">Projects</div>
			{#if filteredProjects.length === 0}
				<div class="res-empty">{q ? 'No matching projects.' : 'No active projects.'}</div>
			{:else}
				{#each filteredProjects as p, i (p.id)}
					<button class="res" class:sel={sel === projBase + i} onmouseenter={() => (sel = projBase + i)} onclick={() => pickProject(p)}>
						<span class="proj-dot" style="background: {projectAccent(p.id, p.accent_color)};"></span>
						<span class="res-label">{p.name}</span>
					</button>
				{/each}
			{/if}

			<div class="g-label">Chats</div>
			{#if filteredConversations.length === 0}
				<div class="res-empty">{q ? 'No matching chats.' : 'No chats.'}</div>
			{:else}
				{#each filteredConversations as c, i (c.id)}
					<button class="res" class:sel={sel === convBase + i} onmouseenter={() => (sel = convBase + i)} onclick={() => pickConversation(c.id)}>
						<span class="res-icon">
							<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
						</span>
						<span class="res-label">{c.title || 'New Chat'}</span>
						{#if c.project_id && projectName[c.project_id]}
							<span class="res-sub">{projectName[c.project_id]}</span>
						{/if}
					</button>
				{/each}
			{/if}
		</div>
	</div>
{/if}

<style>
	.backdrop { position: fixed; inset: 0; background: var(--glass-scrim); backdrop-filter: var(--glass-blur-soft); -webkit-backdrop-filter: var(--glass-blur-soft); z-index: 70; }
	.palette {
		position: fixed; z-index: 71; left: 50%; top: 14vh; transform: translateX(-50%);
		width: min(560px, calc(100vw - 32px)); max-height: 64vh; display: flex; flex-direction: column;
		background: var(--glass-regular-thick, var(--bg-secondary));
		backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
		border: 1px solid var(--border); border-radius: var(--lg-card-radius);
		box-shadow: var(--glass-rim), var(--shadow-chip); overflow: hidden;
	}
	.search { display: flex; align-items: center; gap: 10px; padding: 13px 16px; border-bottom: 1px solid var(--border-subtle); color: var(--text-tertiary); }
	.search input { flex: 1; background: none; border: none; outline: none; font: inherit; font-size: var(--text-input); color: var(--text-primary); caret-color: var(--accent); }
	.pal-close { display: flex; align-items: center; justify-content: center; width: 30px; height: 30px; border-radius: var(--radius-xs); background: none; border: none; color: var(--text-tertiary); cursor: pointer; flex-shrink: 0; }
	.pal-close:hover { background: var(--bg-hover); color: var(--text-primary); }
	.results { overflow-y: auto; padding: 6px; }
	.g-label { font-size: var(--text-caption); font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-tertiary); padding: 10px 8px 4px; }
	.res { display: flex; align-items: center; gap: 10px; width: 100%; text-align: left; padding: 8px 10px; border-radius: var(--radius-sm); background: none; border: none; cursor: pointer; }
	.res:active { scale: none; transition-duration: 0s; }
	.res.sel, .res:active { background: var(--accent); }
	.res.sel .res-label, .res:active .res-label { color: #fff; }
	.res.sel .res-icon, .res:active .res-icon { color: rgba(255,255,255,0.9); }
	.res.sel .res-sub, .res:active .res-sub { color: rgba(255,255,255,0.8); }
	.res.sel .proj-dot, .res:active .proj-dot { box-shadow: 0 0 0 1px rgba(255,255,255,0.6); }
	.res-icon { display: inline-flex; color: var(--text-tertiary); flex-shrink: 0; }
	.res-label { flex: 1; min-width: 0; font-size: var(--text-callout); color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.res-sub { font-size: var(--text-footnote); font-family: var(--font-mono, monospace); color: var(--text-tertiary); flex-shrink: 0; padding-left: 8px; }
	.proj-dot { width: 9px; height: 9px; border-radius: var(--radius-circle); flex-shrink: 0; box-shadow: 0 0 0 1px color-mix(in srgb, var(--text-primary) 8%, transparent); }
	.res-empty { padding: 6px 10px 4px; font-size: var(--text-subhead); color: var(--text-tertiary); }

	@media (max-width: 768px) {
		.palette { left: 0; right: 0; top: 0; transform: none; width: 100%; max-height: 100vh; height: 100%; border-radius: 0; padding-top: var(--safe-top); }
		.results { padding-bottom: calc(6px + env(safe-area-inset-bottom, 0px)); }
		.pal-close { width: 40px; height: 40px; }
	}
</style>
