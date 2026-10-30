<script lang="ts">
	import { agentLabel } from '$lib/stores/measurementAgentLabel';
	import { onMount } from 'svelte';
	import MeasurementAgentsTable from './MeasurementAgentsTable.svelte';
	import MeasurementWhereToLook from './MeasurementWhereToLook.svelte';
	import MeasurementScopePicker, { type PickerAgent } from './MeasurementScopePicker.svelte';
	import MeasurementKeptPanels from './MeasurementKeptPanels.svelte';
	import MeasurementModels from './MeasurementModels.svelte';
	import MeasurementSizeHeadline from './MeasurementSizeHeadline.svelte';
	import MeasurementOutcomeRates from './MeasurementOutcomeRates.svelte';
	import MeasurementCost from './MeasurementCost.svelte';
	import MeasurementKpiRow from './MeasurementKpiRow.svelte';

	let { onClose = null }: {
		/** The doorway that opened this surface; Close returns to exactly that origin (PL-2). */
		onClose?: (() => void) | null;
	} = $props();
	import { projects, loadProjects } from '$lib/stores/projects';
	import { threads, isNavAgent } from '$lib/stores/threads';
	import {
		clearMeasurementAgent,
		filterMeasurementAgent,
		loadMeasurementPanel,
		loadMeasurementPanels,
		measurementPanels,
		measurementScope,
		setMeasurementProject,
		startMeasurementUrlSync,
		type MeasurementScope,
		type PanelState,
	} from '$lib/stores/measurement';
	import { measurementGrain, type MeasurementGrain } from '$lib/stores/measurementGrain';
	import { measurementAgents, measurementAgentsView, loadMeasurementAgents } from '$lib/stores/measurementAgents';
	import { loadMeasurementModels, measurementModels } from '$lib/stores/measurementModels';
	import { loadMeasurementCost, measurementCost } from '$lib/stores/measurementCost';
	import { loadMeasurementWhereToLook, measurementWhereToLook } from '$lib/stores/measurementWhereToLook';
	import { keyFigures, loadKeyFigures } from '$lib/stores/keyFigures';
	import { measurementAgentName } from '$lib/stores/measurementScopeTitle';

	const grains: { id: MeasurementGrain; label: string }[] = [{ id: 'days', label: 'Days' }, { id: 'weeks', label: 'Weeks' }];
	/** The series panels the page loads, each settled before the page can say nothing was recorded. */
	const panelIds = ['outcomes', 'attention', 'asks', 'alignment', 'first-try', 'rejections', 'tokens'];

	/** The workspace bill belongs to All work alone; a project or agent view points back to it. */
	const isAllWork = $derived($measurementScope.project === 'all' && $measurementScope.agent === null);
	const pickerProjects = $derived($projects.map((project) => ({ id: project.id, name: project.name })));
	const pickerAgents = $derived.by(() => {
		const measured = $measurementAgents.rows.map((row) => ({
			id: row.agent.id, name: agentLabel(row.agent.name), projectId: row.agent.project.id, projectName: row.agent.project.name,
		}));
		const measuredIds = new Set(measured.map((row) => row.id));
		const projectNames = new Map($projects.map((row) => [row.id, row.name]));
		const navigationOnly = $threads.filter((thread) => isNavAgent(thread) && !measuredIds.has(thread.id))
			.map((thread) => ({ id: thread.id, name: agentLabel(thread.title), projectId: thread.project_id ?? null, projectName: projectNames.get(thread.project_id ?? '') ?? null }));
		return [...measured, ...navigationOnly] satisfies PickerAgent[];
	});

	onMount(() => { void loadProjects(); return startMeasurementUrlSync(); });
	$effect(() => {
		$measurementGrain;
		$measurementScope;
		void loadMeasurementPanels();
	});
	// The Agents table pools the grain's latest window and the project summaries move with it, so it reloads
	// with the grain; the selected column and order are part of the ask.
	$effect(() => {
		$measurementGrain;
		$measurementScope;
		$measurementAgentsView;
		void loadMeasurementAgents();
	});
	// The Models comparison answers for the same composite scope and grain.
	$effect(() => {
		$measurementGrain;
		$measurementScope;
		// Cost answers at every scope; at All work it carries the workspace bill.
		void loadMeasurementCost();
		void loadMeasurementModels();
	});
	$effect(() => {
		$measurementGrain;
		$measurementScope;
		void loadKeyFigures();
	});
	// Biggest changes compares the last window with the one before it, so the grain moves it.
	$effect(() => {
		$measurementGrain;
		$measurementScope;
		void loadMeasurementWhereToLook();
	});

	function agentProjects(): Map<string, string | null> {
		return new Map($threads.filter(isNavAgent).map((agent) => [agent.id, agent.project_id ?? null]));
	}

	function agentName(scope: MeasurementScope): string {
		return measurementAgentName(scope.agent ?? '', $measurementAgents.rows, $threads);
	}
	function panelState(id: string): PanelState { return $measurementPanels[id] ?? { loading: true, rows: [], error: null, selection: '' }; }
	/** Settled with no recorded activity in any panel or the size headline, including today's or this week's partial period: the cards collapse to one line. */
	const noActivity = $derived.by(() => {
		const recorded = (state: PanelState) => state.rows.some((row) => row.n > 0 || (row.value !== null && Number.isFinite(row.value)));
		const size = panelState('size');
		return !size.loading && !size.error && !recorded(size) && panelIds.every((id) => {
			const state = panelState(id);
			return !state.loading && !state.error && !recorded(state);
		});
	});
</script>

<div class="measurement-page">
	<header class="header">
		<div class="controls">
			<span class="scope-label">Showing</span>
			<MeasurementScopePicker
				projects={pickerProjects}
				agents={pickerAgents}
				project={$measurementScope.project}
				agent={$measurementScope.agent}
				onSelectProject={(id) => setMeasurementProject(id, agentProjects())}
				onSelectAgent={(row) => filterMeasurementAgent(row.id, row.projectId)}
			/>
			{#if $measurementScope.agent}<span class="agent-chip">Agent: {agentName($measurementScope)} <button aria-label="Remove agent filter" onclick={clearMeasurementAgent}>×</button></span>{/if}
			<div class="seg" role="group" aria-label="Grain">{#each grains as grain}<button type="button" class:on={$measurementGrain === grain.id} aria-pressed={$measurementGrain === grain.id} onclick={() => measurementGrain.set(grain.id)}>{grain.label}</button>{/each}</div>
			{#if onClose}<button class="close-page" aria-label="Close Measurement" onclick={onClose}>Close</button>{/if}
		</div>
	</header>

	<MeasurementKpiRow state={$keyFigures} onRetry={() => void loadKeyFigures()} />

	{#if noActivity}
		<p class="headlines-pending">{$measurementGrain === 'days' ? 'Daily numbers start with your first recorded day.' : 'Weekly numbers start with your first recorded week.'}</p>
	{:else}
		<MeasurementSizeHeadline state={panelState('size')} onRetry={() => void loadMeasurementPanel('size')} />
	{/if}

	<section aria-label="Cost"><MeasurementCost state={$measurementCost} onRetry={() => void loadMeasurementCost()} scoped={!isAllWork} onOpenAllWork={() => measurementScope.set({ project: 'all', agent: null })}>
		<section aria-label="Models comparison"><MeasurementModels state={$measurementModels} /></section>
	</MeasurementCost></section>

	{#if !noActivity}
	<MeasurementOutcomeRates attention={panelState('attention')} asks={panelState('asks')} firstTry={panelState('first-try')} onRetry={(panel) => void loadMeasurementPanel(panel)} />
	<MeasurementKeptPanels outcomes={panelState('outcomes')} alignment={panelState('alignment')} asks={panelState('asks')} rejections={panelState('rejections')} tokens={panelState('tokens')} onRetry={(panel) => void loadMeasurementPanel(panel)} />
	{/if}

	<MeasurementWhereToLook state={$measurementWhereToLook} onRetry={() => void loadMeasurementWhereToLook()}>
		<MeasurementAgentsTable state={$measurementAgents} embedded />
	</MeasurementWhereToLook>
</div>

<style>
	/* The render's colours for this page. The app tokens the Measurement cards read are re-pointed here, so every card, control and chart on the page takes them from one place.
	   The scroller that hosts the page takes the same page colour, so the gutters around the bounded page are not a second band. */
	:global(.measurement-scroller:has(> .measurement-page)), .measurement-page {
		--mm-page: #f3f5f8; --mm-card: #fff; --mm-line: #dde3ea; --mm-grid: #e3e8ee; --mm-ink: #1b2430; --mm-muted: #5b6675;
		--mm-bar: #9aa4b2; --mm-current: #2f6fdb; --mm-on-current: #fff; --mm-neutral: #d5dbe3; --mm-tile: #f7f9fb;
		--text-primary: var(--mm-ink); --text-secondary: var(--mm-muted); --text-tertiary: var(--mm-muted);
		--bg-secondary: var(--mm-card); --bg-primary: var(--mm-card); --bg-hover: var(--mm-tile); --border-subtle: var(--mm-line);
		--blue: var(--mm-current);
		background: var(--mm-page); color: var(--mm-ink);
	}
	:global([data-theme="dark"] .measurement-scroller:has(> .measurement-page)), :global([data-theme="dark"]) .measurement-page {
		--mm-page: #12161c; --mm-card: #1a2029; --mm-line: #2c3541; --mm-grid: #2c3541; --mm-ink: #e6ebf2; --mm-muted: #9aa6b6;
		--mm-bar: #6f7b8b; --mm-current: #5b8fe8; --mm-on-current: #12161c; --mm-neutral: #3a4553; --mm-tile: #202732;
	}
	.measurement-page :global(h3) { font-weight: 700; }
	/* r2.7's bounded page: at most 1200px, centred; the host's gutter is at least the cards' 14px padding, and sections sit that gutter apart. */
	.measurement-page { display: flex; flex-direction: column; gap: 20px; width: 100%; max-width: 1200px; margin: 0 auto; } @media (max-width: 768px) { .measurement-page { gap: 14px; } } .header { display: flex; justify-content: flex-end; align-items: flex-start; gap: 16px; flex-wrap: wrap; } p { margin: 0; } .scope-label { color: var(--text-secondary); font-size: var(--text-footnote); } p { color: var(--text-secondary); font-size: var(--text-subhead); margin-top: 4px; } .controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; } .seg { display: inline-flex; overflow: hidden; border: 1px solid var(--border-subtle); border-radius: var(--radius-xs); background: var(--bg-primary); } .seg button { min-height: 32px; padding: 5px 14px; border: none; background: var(--bg-primary); color: var(--text-primary); font: inherit; font-size: var(--text-subhead); cursor: pointer; } .seg button.on { background: var(--text-primary); color: var(--bg-primary); } .agent-chip { display: inline-flex; align-items: center; gap: 4px; color: var(--text-primary); background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: var(--radius-xs); padding: 4px 8px; font-size: var(--text-footnote); } .agent-chip button { border: none; background: none; color: var(--text-secondary); font-size: var(--text-callout); line-height: 1; cursor: pointer; } .close-page { padding: 5px 11px; border: 1px solid var(--border-subtle); border-radius: var(--radius-xs); background: var(--bg-secondary); color: var(--text-secondary); font: inherit; font-size: var(--text-footnote); cursor: pointer; } .close-page:hover { background: var(--bg-hover); color: var(--text-primary); } .headlines-pending { margin-top: 0; } @media (max-width: 768px) { .controls { width: 100%; } } /* On a phone the Scope label pushes the picker off the page edge, so its panel anchors to the controls row instead and stays inside the viewport. */ @media (max-width: 768px) { .controls { position: relative; } .controls :global(.scope-picker) { position: static; } }
</style>
