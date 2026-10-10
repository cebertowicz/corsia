<script lang="ts">
	/**
	 * One searchable, grouped, keyboard-operable scope picker.
	 *
	 * The popover is anchored to the right edge of its wrapper and sized against
	 * the viewport, so it stays inside the page column at 393 and 1440 instead of
	 * running past it the way a fixed-width popover did. Every agent result keeps
	 * its project name visible, wrapped rather than clipped, so same-named agents
	 * in different projects stay distinguishable.
	 *
	 * At agent scope the trigger names the agent's parent project and the chip
	 * beside it names the agent: the trigger answers "where am I looking", the
	 * chip answers "what is filtered out if I remove this".
	 */
	import { tick } from 'svelte';

	export type PickerProject = { id: string; name: string };
	export type PickerAgent = { id: string; name: string; projectId: string | null; projectName: string | null };
	type PickerOption = {
		id: string;
		label: string;
		detail: string | null;
		kind: 'all' | 'project' | 'agent';
		projectId: string | null;
		agentId: string | null;
	};

	let { projects, agents, project = 'all', agent = null, onSelectProject, onSelectAgent }: {
		projects: PickerProject[];
		agents: PickerAgent[];
		project?: string | 'all';
		agent?: string | null;
		onSelectProject: (id: string | 'all') => void;
		onSelectAgent: (agent: PickerAgent) => void;
	} = $props();

	let open = $state(false);
	let search = $state('');
	let activeIndex = $state(0);
	let input = $state<HTMLInputElement | undefined>(undefined);
	let listbox = $state<HTMLElement | undefined>(undefined);

	const LISTBOX_ID = 'measurement-scope-options';

	const allOption: PickerOption = { id: 'scope-all', label: 'All work', detail: null, kind: 'all', projectId: null, agentId: null };
	const agentOption = (row: PickerAgent): PickerOption => ({
		id: `scope-agent-${row.id}`, label: row.name, detail: row.projectName ?? 'No project',
		kind: 'agent', projectId: row.projectId, agentId: row.id,
	});
	const projectOption = (row: PickerProject): PickerOption => ({
		id: `scope-project-${row.id}`, label: row.name, detail: 'Project', kind: 'project', projectId: row.id, agentId: null,
	});

	const groups = $derived.by(() => {
		// Keep the search read in this derived expression. The grouped options
		// must recompute as a person types, rather than retain a captured query.
		const query = search.trim().toLowerCase();
		const matches = (value: string | null) => !query || (value ?? '').toLowerCase().includes(query);
		const owned = new Set<string>();
		const result = projects
			.map((item) => {
				const ownAgents = agents.filter((row) => {
					if (row.projectId !== item.id) return false;
					owned.add(row.id);
					return matches(row.name) || matches(item.name);
				});
				return { project: item, show: matches(item.name) || ownAgents.length > 0, ownAgents };
			})
			.filter((group) => group.show);
		const orphans = agents
			.filter((row) => !owned.has(row.id) && !projects.some((item) => item.id === row.projectId))
			.filter((row) => matches(row.name) || matches(row.projectName))
			.map(agentOption);
		return {
			all: !query || 'all work'.includes(query) ? [allOption] : [],
			projects: result,
			orphans,
		};
	});
	const options = $derived<PickerOption[]>([
		...groups.all,
		...groups.projects.flatMap((group) => [projectOption(group.project), ...group.ownAgents.map(agentOption)]),
		...groups.orphans,
	]);
	const activeOption = $derived(options[Math.min(activeIndex, Math.max(options.length - 1, 0))]);
	const selectedLabel = $derived.by(() => {
		const chosen = agents.find((row) => row.id === agent);
		if (chosen) return chosen.projectName ?? 'No project';
		if (project === 'all') return 'All work';
		return projects.find((row) => row.id === project)?.name ?? 'Selected project';
	});

	async function show() {
		open = true;
		activeIndex = 0;
		await tick();
		input?.focus();
	}
	function hide() {
		open = false;
		search = '';
	}
	function choose(option: PickerOption | undefined) {
		if (!option) return;
		if (option.kind === 'agent') {
			const row = agents.find((candidate) => candidate.id === option.agentId);
			if (row) onSelectAgent(row);
		} else {
			onSelectProject(option.projectId ?? 'all');
		}
		hide();
	}
	function move(step: number) {
		if (!options.length) return;
		activeIndex = (activeIndex + step + options.length) % options.length;
		scrollActiveIntoView();
	}
	function scrollActiveIntoView() {
		void tick().then(() => {
			listbox?.querySelector('[data-active="true"]')?.scrollIntoView({ block: 'nearest' });
		});
	}
	function onKeydown(event: KeyboardEvent) {
		if (event.key === 'ArrowDown') { event.preventDefault(); move(1); }
		else if (event.key === 'ArrowUp') { event.preventDefault(); move(-1); }
		else if (event.key === 'Home') { event.preventDefault(); activeIndex = 0; scrollActiveIntoView(); }
		else if (event.key === 'End') { event.preventDefault(); activeIndex = Math.max(options.length - 1, 0); scrollActiveIntoView(); }
		else if (event.key === 'Enter') { event.preventDefault(); choose(activeOption); }
		else if (event.key === 'Escape') { event.preventDefault(); hide(); }
	}
</script>

<div class="scope-picker">
	<button
		class="picker-trigger"
		aria-haspopup="listbox"
		aria-expanded={open}
		aria-label="Measurement scope: {selectedLabel}"
		onclick={() => (open ? hide() : show())}
	>
		<span class="picker-label">{selectedLabel}</span>
		<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>
	</button>

	{#if open}
		<button class="picker-backdrop" tabindex="-1" aria-label="Close scope picker" onclick={hide}></button>
		<div class="picker-panel">
			<input
				bind:this={input}
				bind:value={search}
				class="picker-search"
				type="search"
				role="combobox"
				aria-expanded="true"
				aria-controls={LISTBOX_ID}
				aria-activedescendant={activeOption?.id}
				aria-autocomplete="list"
				aria-label="Search projects and agents"
				placeholder="Search projects and agents"
				oninput={() => (activeIndex = 0)}
				onkeydown={onKeydown}
			/>
			<div class="picker-list" bind:this={listbox} id={LISTBOX_ID} role="listbox" aria-label="Measurement scopes">
				{#each groups.all as option (option.id)}
					{@const active = option.id === activeOption?.id}
					<button id={option.id} class="picker-option" class:active role="option" aria-selected={active} data-active={active} tabindex="-1" onclick={() => choose(option)}>
						<span class="option-label">{option.label}</span>
					</button>
				{/each}
				{#each groups.projects as group (group.project.id)}
					{@const projectChoice = projectOption(group.project)}
					{@const projectActive = projectChoice.id === activeOption?.id}
					<div class="picker-group" role="group" aria-label={group.project.name}>
						<button id={projectChoice.id} class="picker-option is-project" class:active={projectActive} role="option" aria-selected={projectActive} data-active={projectActive} tabindex="-1" onclick={() => choose(projectChoice)}>
							<span class="option-label">{projectChoice.label}</span>
							<span class="option-detail">All agents in this project</span>
						</button>
						{#each group.ownAgents as row (row.id)}
							{@const option = agentOption(row)}
							{@const active = option.id === activeOption?.id}
							<button id={option.id} class="picker-option is-nested" class:active role="option" aria-selected={active} data-active={active} tabindex="-1" onclick={() => choose(option)}>
								<span class="option-label">{option.label}</span>
								<span class="option-detail">{option.detail}</span>
							</button>
						{/each}
					</div>
				{/each}
				{#each groups.orphans as option (option.id)}
					{@const active = option.id === activeOption?.id}
					<button id={option.id} class="picker-option" class:active role="option" aria-selected={active} data-active={active} tabindex="-1" onclick={() => choose(option)}>
						<span class="option-label">{option.label}</span>
						<span class="option-detail">{option.detail}</span>
					</button>
				{/each}
				{#if !options.length}<p class="picker-empty">No project or agent matches that search.</p>{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	.scope-picker { position: relative; display: inline-flex; }
	.picker-trigger {
		display: inline-flex; align-items: center; gap: 6px; max-width: 100%;
		padding: 5px 8px; border: 1px solid var(--border-subtle); border-radius: var(--radius-xs);
		background: var(--bg-primary); color: var(--text-primary); cursor: pointer; font: inherit; font-size: var(--text-footnote);
	}
	.picker-trigger:hover { background: var(--bg-hover); }
	.picker-label { overflow-wrap: anywhere; text-align: left; }
	.picker-backdrop { position: fixed; inset: 0; z-index: 39; border: none; background: transparent; padding: 0; cursor: default; }
	.picker-panel {
		position: absolute; top: calc(100% + 4px); right: 0; z-index: 40;
		display: flex; flex-direction: column; gap: 6px; box-sizing: border-box;
		width: min(420px, calc(100vw - 32px)); padding: 8px;
		background: var(--bg-primary); border: 1px solid var(--border-subtle); border-radius: var(--radius);
		box-shadow: var(--shadow-chip, 0 8px 32px rgba(0, 0, 0, .32));
	}
	.picker-search {
		box-sizing: border-box; width: 100%; padding: 6px 8px; color: var(--text-primary);
		background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: var(--radius-xs); font-size: var(--text-footnote);
	}
	.picker-list { max-height: min(320px, 45vh); overflow-y: auto; display: flex; flex-direction: column; gap: 2px; }
	.picker-group { display: flex; flex-direction: column; gap: 1px; padding-top: 4px; }
	.picker-option {
		box-sizing: border-box; display: flex; flex-direction: column; align-items: flex-start; gap: 1px; width: 100%;
		padding: 6px 8px; border: none; border-radius: var(--radius-xs); background: none;
		color: var(--text-primary); cursor: pointer; font: inherit; font-size: var(--text-footnote); text-align: left;
	}
	.picker-option:hover, .picker-option.active { background: var(--bg-hover); }
	.picker-option.is-project { color: var(--text-secondary); border-top: 1px solid var(--border-subtle); border-radius: var(--radius-xs) var(--radius-xs) 0 0; }
	/* Highlighting paints --bg-hover under the row, and a project header row is itself secondary,
	   which measures 4.15:1 in light. A highlighted row carries primary text instead. */
	.picker-option:hover, .picker-option.active { color: var(--text-primary); }
	.picker-option.is-nested { padding-left: 16px; }
	.option-label { overflow-wrap: anywhere; }
	/* The project name is the disambiguator for same-named agents: wrap it, never clip it. */
	.option-detail { color: var(--text-secondary); overflow-wrap: anywhere; }
	/* A highlighted row sits on --bg-hover, where secondary drops to 4.15:1 in light, so the
	   detail line follows the row's own text colour while it is hovered or selected. */
	.picker-option:hover .option-detail, .picker-option.active .option-detail { color: inherit; }
	.picker-empty { margin: 4px 8px; color: var(--text-secondary); font-size: var(--text-footnote); }
	/* The browser's own placeholder grey measured 4.41:1 in light and 3.99:1 in dark against the
	   field, so the sheet names the token instead of leaving the hint colour to the user agent. */
	.picker-search::placeholder { color: var(--text-secondary); opacity: 1; }
	@media (max-width: 768px) {
		/* The panel is wider than the trigger here, so right-anchoring it pushes its
		   left edge outside the viewport. Anchor it to the trigger's left instead. */
		.picker-panel { left: 0; right: auto; }
	}
	@media (max-width: 393px) {
		/* At this breakpoint controls fill the page column, so the panel does too. */
		.scope-picker { display: flex; width: 100%; }
		.picker-trigger { justify-content: space-between; width: 100%; max-width: none; }
		.picker-panel { left: 0; right: 0; width: auto; max-width: none; }
	}
</style>
