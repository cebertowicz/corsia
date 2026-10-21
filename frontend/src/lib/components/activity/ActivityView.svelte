<script lang="ts">
	import { threads } from '$lib/stores/threads';
	import { threadActivity } from '$lib/stores/activity';
	import {
		projects,
		projectLoadStatus,
		projectLoadError,
		loadProjects,
		enterProjectScope,
	} from '$lib/stores/projects';
	import { paletteOpen } from '$lib/stores/palette';
	import { token } from '$lib/stores/auth';
	import { connectionState } from '$lib/stores/connection';
	import { activityQuery } from '$lib/stores/planQuery.svelte';
	import type { ActivityKey } from '$lib/stores/planKeys';
	import {
		buildActivityGroups,
		buildResumeThreads,
		nextPromotionExpiry,
	} from '$lib/activityHome';
	import Section from '$lib/components/ui/Section.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import ActivityGroupBlock from './ActivityGroupBlock.svelte';
	import ActivityScopeState, { type ActivityScopeStateKind } from './ActivityScopeState.svelte';
	import ActivityResumeList from './ActivityResumeList.svelte';
	import { openDecisionFromActivity, reportOpenDecision } from '$lib/stores/attentionDock.svelte';
	import { openMeasurementFromProject } from '$lib/stores/measurementNavigation';

	const openDecision = async (threadId: string | null = null) =>
		reportOpenDecision(await openDecisionFromActivity(onSelectThread, threadId));

	let {
		projectId = null,
		scopeName,
		onSelectThread,
		onNeedsYou,
		onOpenChild = () => {},
		onOpenLegacyDiscussion = () => {},
		onGoWorkspace = () => {},
		onMeasurement = () => {},
	}: {
		projectId?: string | null;
		scopeName: string;
		onSelectThread: (threadId: string) => void | Promise<void>;
		onNeedsYou?: () => void;
		onOpenChild?: (
			parentThreadId: string,
			nodeId: string | null,
			childThreadId: string,
			title: string,
			stepId?: string,
		) => void | Promise<void>;
		onOpenLegacyDiscussion?: (
			parentThreadId: string,
			artifactId: string,
			title: string,
		) => void | Promise<void>;
		onGoWorkspace?: () => void;
		/** PL-3: the project Activity doorway opens project scope. */
		onMeasurement?: () => void;
	} = $props();

	function openMeasures() {
		if (projectId) openMeasurementFromProject(projectId);
		onMeasurement();
	}

	const key: ActivityKey = $derived(
		projectId ? { kind: 'activity-project', id: projectId } : { kind: 'activity-workspace' },
	);
	const query = activityQuery(() => key);
	const hasPayload = $derived(query.payload != null);
	const offline = $derived($connectionState !== 'connected' && hasPayload);
	const resumeThreads = $derived(buildResumeThreads(
		$threads,
		$threadActivity,
		projectId ? { projectId, projects: $projects } : { projects: $projects },
	));
	let orderingNow = $state(Date.now());
	const groups = $derived(buildActivityGroups(
		query.payload?.plans ?? [],
		$threads,
		$projects,
		projectId ? { projectId, now: orderingNow } : { now: orderingNow },
	));
	const promotionExpiry = $derived(nextPromotionExpiry(groups, orderingNow));
	const decisionCount = $derived(query.payload?.attention.awaiting_count ?? 0);

	/**
	 * Which non-content state to show, or null to render the rollup. A stale or
	 * failed refresh over an existing payload keeps the payload and gets a note
	 * instead, so a transient failure never blanks work the reader was looking at.
	 */
	const scopeStateKind = $derived.by((): ActivityScopeStateKind | null => {
		if (hasPayload) return null;
		switch (query.status) {
			case 'loading':
			case 'unauthorized':
			case 'forbidden':
			case 'not-found':
			case 'error':
				return query.status;
			default:
				return null;
		}
	});

	let waitingOpen = $state(false);
	let completeOpen = $state(false);
	const waitingCount = $derived(groups.reduce((n, group) => n + group.waiting.length, 0));
	const completeCount = $derived(groups.reduce((n, group) => n + group.complete.length, 0));

	$effect(() => {
		if ($token && $projectLoadStatus === 'idle') void loadProjects();
	});

	$effect(() => {
		if (promotionExpiry === null) return;
		const timer = setTimeout(() => {
			orderingNow = Date.now();
		}, Math.max(0, promotionExpiry - Date.now()));
		return () => clearTimeout(timer);
	});

	function projectName(id: string | null): string | null {
		if (!id) return null;
		// Omit the suffix rather than claiming a generic "Project" name we do not
		// actually know yet.
		return $projects.find((project) => project.id === id)?.name ?? null;
	}
</script>

<div class="activity-scroll">
	<div class="activity">
		<header class="activity-title">
			<h1>Activity</h1>
			<span>{scopeName}</span>
			{#if projectId}
				<button class="activity-measures press-tint" onclick={openMeasures}>Measures</button>
			{/if}
		</header>

		{#if $projectLoadError}
			<!-- The catalog supplies project names for Continue rows and group
			     headers. When it fails, those names are simply absent, so say that
			     rather than letting rows look unaffiliated. Plan data is separate
			     and still shown. -->
			<div class="stale-note" role="status">
				<span>Project names couldn’t load, so some rows show without one.</span>
				<Button variant="ghost" size="sm" onclick={() => loadProjects({ force: true })}>Retry</Button>
			</div>
		{/if}

		<Section title="Continue">
			<ActivityResumeList threads={resumeThreads} {projectName} {onSelectThread} />
		</Section>

		{#if decisionCount > 0}
			<Section title="Needs you">
				<button class="needs-you" onclick={() => (onNeedsYou ? onNeedsYou() : void openDecision())}>
					<span class="needs-you-label">
						<span class="needs-you-mark" aria-hidden="true"></span>
						<span>
							<strong>{decisionCount}</strong>
							{decisionCount === 1 ? 'decision' : 'decisions'}
						</span>
					</span>
					<span>Open ›</span>
				</button>
			</Section>
		{/if}

		<section class="activity-work" aria-labelledby="activity-work-title">
			<h2 id="activity-work-title">In progress</h2>

			{#if scopeStateKind}
				<ActivityScopeState
					kind={scopeStateKind}
					canGoWorkspace={projectId !== null}
					onRetry={() => query.retry()}
					{onGoWorkspace}
				/>
			{:else}
				{#if query.status === 'stale' || offline}
					<div class="stale-note" role="status">
						<span>{offline
							? "You're offline — showing last known activity."
							: 'Showing last known activity.'}</span>
						{#if !offline}
							<Button variant="ghost" size="sm" onclick={() => query.retry()}>Retry</Button>
						{/if}
					</div>
				{/if}

				{#if groups.length === 0}
					<ActivityScopeState
						kind="empty"
						onRetry={() => query.retry()}
						{onGoWorkspace}
					/>
				{:else}
					{#each groups.filter((g) => g.current.length > 0) as group (group.id)}
						<ActivityGroupBlock {group} kind="current" showHead={projectId === null}
							{onSelectThread} onOpenDecision={openDecision} {onOpenChild}
							{onOpenLegacyDiscussion} />
					{:else}
						<p class="quiet">Nothing in progress.</p>
					{/each}
				{/if}
			{/if}
		</section>

		{#if !scopeStateKind && waitingCount > 0}
			<section class="activity-work waiting-work" aria-labelledby="activity-waiting-title">
				<h2 id="activity-waiting-title">
					<button
						class="section-toggle waiting-toggle"
						type="button"
						onclick={() => (waitingOpen = !waitingOpen)}
						aria-expanded={waitingOpen}
					>
						<span aria-hidden="true">{waitingOpen ? '⌄' : '›'}</span>
						Waiting ({waitingCount})
					</button>
				</h2>
				{#if waitingOpen}
					{#each groups.filter((g) => g.waiting.length > 0) as group (group.id)}
						<ActivityGroupBlock {group} kind="waiting" showHead={projectId === null}
							{onSelectThread} onOpenDecision={openDecision} {onOpenChild}
							{onOpenLegacyDiscussion} />
					{/each}
				{/if}
			</section>
		{/if}

		{#if !scopeStateKind && completeCount > 0}
			<section class="activity-work waiting-work complete-work" aria-labelledby="activity-complete-title">
				<h2 id="activity-complete-title">
					<button
						class="section-toggle complete-toggle"
						type="button"
						onclick={() => (completeOpen = !completeOpen)}
						aria-expanded={completeOpen}
					>
						<span aria-hidden="true">{completeOpen ? '⌄' : '›'}</span>
						Complete ({completeCount})
					</button>
				</h2>
				{#if completeOpen}
					{#each groups.filter((g) => g.complete.length > 0) as group (group.id)}
						<ActivityGroupBlock {group} kind="complete" showHead={projectId === null}
							{onSelectThread} onOpenDecision={openDecision} {onOpenChild}
							{onOpenLegacyDiscussion} />
					{/each}
				{/if}
			</section>
		{/if}

		<div>
			{#if projectId === null}
				<!-- A project with no plans produces no group, so it would otherwise be
				     unreachable from here. One quiet affordance opens the existing
				     palette, which already lists every authorized project. -->
				<button class="all-projects" type="button" onclick={() => paletteOpen.set(true)}>
					All projects…
				</button>
			{/if}
		</div>
	</div>
</div>

<style>
	.activity-scroll {
		flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain;
	}
	.activity {
		width: min(780px, calc(100% - 40px)); margin: 0 auto;
		padding: calc(var(--header-bottom) + var(--space-6)) 0 calc(var(--space-8) + env(safe-area-inset-bottom));
	}
	.activity-title {
		display: flex; align-items: baseline; gap: var(--space-3); margin-bottom: var(--space-5);
	}
	.activity-title h1 {
		margin: 0; color: var(--text-primary); font-size: var(--text-title1);
		font-weight: var(--fw-semibold); letter-spacing: -0.025em;
	}
	.activity-title span { color: var(--text-tertiary); font-size: var(--text-subhead); }
	.activity-measures {
		margin-left: auto; padding: 5px 11px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm);
		background: none; color: var(--text-secondary); cursor: pointer;
		font-size: var(--text-footnote); font-weight: var(--fw-medium); white-space: nowrap;
	}
	/* Hover is for a pointer: a tap leaves :hover stuck on the last target. */
	@media (hover: hover) {
		.activity-measures:hover { background: var(--bg-hover); color: var(--text-primary); }
		.all-projects:hover, .section-toggle:hover { color: var(--text-primary); }
	}
	/* Needs you is the chat view's frosted card with its amber mark (AttentionDock). */
	.needs-you {
		display: flex; align-items: center; justify-content: space-between; width: 100%; min-height: 52px;
		padding: var(--space-3); border: 1px solid var(--border); border-radius: var(--radius-xl);
		background: var(--glass-thin); box-shadow: var(--glass-rim), var(--shadow-chip);
		color: var(--text-primary); font: inherit; text-align: left; cursor: pointer;
		transition: background var(--dur-tight) var(--ease-snap);
	}
	.needs-you-label { display: flex; align-items: center; gap: var(--space-2); }
	.needs-you-mark { width: 8px; height: 8px; flex: none; border-radius: var(--radius-circle); background: var(--amber); }
	.needs-you strong { font-variant-numeric: tabular-nums; }
	.needs-you > span:last-child { color: var(--text-tertiary); font-size: var(--text-caption); transition: color var(--dur-tight) var(--ease-snap); }
	@media (hover: hover) {
		.needs-you:hover { background: var(--bg-hover); }
		.needs-you:hover > span:last-child { color: var(--text-primary); }
	}
	.needs-you:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.activity-work { margin-top: var(--space-6); }
	.activity-work > h2 {
		margin: 0 0 var(--space-2); color: var(--text-tertiary); font-size: var(--text-caption);
		font-weight: var(--fw-bold); letter-spacing: 0.12em; text-transform: uppercase;
	}
	.all-projects {
		display: flex; align-items: center; min-height: 38px; margin-top: var(--space-3);
		padding: var(--space-1) 0; border: 0; background: transparent;
		color: var(--text-tertiary); font: inherit; font-size: var(--text-caption); cursor: pointer;
	}
	.all-projects:focus-visible {
		outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs);
	}
	.quiet { margin: 0; padding: var(--space-3) 0; color: var(--text-tertiary); font-size: var(--text-footnote); }
	.waiting-work { margin-top: var(--space-4); }
	/* Waiting and Complete are the same disclosure under the same caps heading. */
	.activity-work > h2 > .section-toggle {
		display: flex; align-items: center; gap: var(--space-1); min-height: 38px; padding: 0;
		border: 0; background: transparent; color: inherit; font: inherit; letter-spacing: inherit;
		text-transform: inherit; cursor: pointer;
	}
	.section-toggle:focus-visible {
		outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs);
	}
	.stale-note {
		display: flex; align-items: center; justify-content: space-between; gap: var(--space-2);
		min-height: 40px; padding: var(--space-2) 0; color: var(--text-secondary); font-size: var(--text-caption);
	}
	@media (max-width: 620px) {
		.activity { width: calc(100% - 28px); padding-top: calc(var(--header-bottom) + var(--space-4)); }
		.activity-title { margin-bottom: var(--space-4); }
		.activity-title h1 { font-size: var(--text-title2); }
		.needs-you, .activity-work > h2 > .section-toggle, .all-projects, .activity-measures { min-height: 44px; }
	}
</style>
