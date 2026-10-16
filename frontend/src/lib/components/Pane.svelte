<script lang="ts">
	import PaneShell from './PaneShell.svelte';
	import ArtifactPane from './ArtifactPane.svelte';
	import ThreadPlanPane from './plans/ThreadPlanPane.svelte';
	import PlanHeadControls from './plans/PlanHeadControls.svelte';
	import { paneOpen } from '$lib/stores/paneOpen';
	import { paneCollapsed } from '$lib/stores/paneCollapsed';
	import { projects } from '$lib/stores/projects';
	import { createProjectOpen } from '$lib/stores/overlays';
	import { projectAccent } from '$lib/utils/projectColors';
	import { activeThreadId } from '$lib/stores/threads';
	import { isMobile } from '$lib/stores/viewport';
	import type { Section } from '$lib/homePaneConfig';
	import { openPlanDiscussionLeaf, openPlanSubagentLeaf } from '$lib/planLeafReturn';
	import { registerPlanSurface } from '$lib/planNodeFocus';

	let { label = 'Workspace', sections = [] as Section[], conductorThreadId = null, protectArtifactSplit = false, onPickProject, onBrowseDocuments }: {
		label?: string;
		sections?: Section[];
		conductorThreadId?: string | null;
		protectArtifactSplit?: boolean;
		onPickProject?: (p: { id: string; name: string }) => void;
		onBrowseDocuments?: (threadId: string) => void;
	} = $props();

	const activeProjects = $derived($projects.filter((p) => p.status === 'active'));

	let open = $state<Record<string, boolean>>({});
	function toggle(s: Section) { open[s.title] = !isOpen(s); }
	function isOpen(s: Section) { return open[s.title] ?? s.initialOpen ?? true; }
	function capMeta(s: Section): string {
		if (s.kind === 'projects') return activeProjects.length ? String(activeProjects.length) : '';
		return s.meta ?? '';
	}

	// An attention item opens the desktop plan here; a phone's plan lives in its chat drawer.
	$effect(() => registerPlanSurface(() => {
		const planSection = sections.find((section) => section.kind === 'plan');
		if ($isMobile || !planSection) return false;
		open[planSection.title] = true;
		paneCollapsed.set(false);
		paneOpen.set(true);
		return true;
	}));

	function openSubagentBesideParent(nodeId: string, childThreadId: string, title: string, stepId?: string): void {
		openPlanSubagentLeaf($activeThreadId, nodeId, childThreadId, title, stepId);
	}

	function openLegacyDiscussion(artifactId: string, title: string): void {
		openPlanDiscussionLeaf($activeThreadId, artifactId, title);
	}
</script>

<PaneShell {label} {protectArtifactSplit}>
	{#each sections as s (s.title)}
		{#if s.kind !== 'plan' || !$isMobile}
		<section class="group">
				<div class="cap-row">
					<button class="cap press-tint" onclick={() => toggle(s)}>
						<svg class="chev" class:open={isOpen(s)} width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>
						<span>{s.title}</span>{#if capMeta(s)}<span class="cap-meta">{capMeta(s)}</span>{/if}
					</button>
					<!-- The plan's count and Pause sit on its cap, so the card holds only the plan. -->
					{#if s.kind === 'plan' && $activeThreadId}
						<span class="cap-actions"><PlanHeadControls threadId={$activeThreadId} /></span>
					{/if}
				</div>
				{#if isOpen(s)}
					<div class="card">
						{#if s.kind === 'projects'}
							{#if activeProjects.length === 0}
								<div class="stub-row">{s.empty ?? 'No active projects'}</div>
							{:else}
								<div class="proj-list">
									{#each activeProjects as p (p.id)}
										<button class="proj-row press-tint" onclick={() => onPickProject?.({ id: p.id, name: p.name })}>
											<span class="proj-dot" style="background: {projectAccent(p.id, p.accent_color)};"></span>
											<span class="proj-name">{p.name}</span>
										</button>
									{/each}
								</div>
							{/if}
							<button class="proj-new press-tint" onclick={() => createProjectOpen.set(true)}>
								<span class="proj-new-plus" aria-hidden="true">+</span>
								<span>New project</span>
							</button>
						{:else if s.kind === 'plan'}
							{#if $activeThreadId}
								<!-- The section cap names the plan and carries its count and Pause. -->
								<ThreadPlanPane
									threadId={$activeThreadId}
									onOpenChild={openSubagentBesideParent}
									onOpenLegacyDiscussion={openLegacyDiscussion}
								/>
							{:else}
								<div class="stub-row">Open a conversation to see its plan</div>
							{/if}
						{:else if s.kind === 'artifacts'}
							<ArtifactPane threadId={$activeThreadId} {conductorThreadId} {onBrowseDocuments} />
						{:else}
							<div class="stub-row">{s.empty ?? 'Nothing here yet'}</div>
						{/if}
					</div>
				{/if}
		</section>
		{/if}
	{/each}
</PaneShell>

<style>
	.group { margin-bottom: 18px; }
	.group:last-child { margin-bottom: 4px; }
	.cap-row { display: flex; align-items: center; gap: 6px; }
	.cap-actions { flex: none; padding: 0 8px 8px 0; }
	.cap {
		display: flex; flex: 1; min-width: 0; align-items: center; gap: 6px; width: 100%; text-align: left;
		background: none; border: none; cursor: pointer; padding: 2px 8px 8px;
		font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-tertiary);
	}
	.cap-meta { margin-left: auto; font-family: var(--font-mono); font-weight: var(--fw-semibold); letter-spacing: 0; text-transform: none; }
	.chev { flex-shrink: 0; transition: transform var(--dur-tight) ease; }
	.chev.open { transform: rotate(90deg); }
	/* Groups sit on the pane's own surface: spacing and the section label do the grouping, in both themes. */
	.card { background: none; border: none; border-radius: var(--lg-card-radius); overflow: hidden; }
	.stub-row {
		padding: 11px 14px; font-size: var(--text-subhead); color: var(--text-tertiary);
	}
	.proj-row {
		display: flex; align-items: center; gap: 10px; width: 100%; text-align: left;
		padding: 10px 14px; background: none; border: none; cursor: pointer;
		color: var(--text-primary); font-family: inherit; font-size: var(--text-subhead);
		transition: background var(--dur-tight);
	}
	.proj-row:hover { background: var(--lg-row-hover, var(--bg-hover)); }
	.proj-dot { width: 9px; height: 9px; border-radius: var(--radius-circle); flex-shrink: 0; box-shadow: 0 0 0 1px color-mix(in srgb, var(--text-primary) 8%, transparent); }
	.proj-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.proj-list { max-height: 280px; overflow-y: auto; }
	.proj-new {
		display: flex; align-items: center; gap: 9px; width: 100%; text-align: left;
		padding: 10px 14px; background: none; border: none; cursor: pointer;
		color: var(--text-tertiary); font-family: inherit; font-size: var(--text-subhead);
		border-top: 1px solid var(--border-subtle);
		transition: background var(--dur-tight), color var(--dur-tight);
	}
	.proj-new:hover { background: var(--lg-row-hover, var(--bg-hover)); color: var(--accent-text); }
	.proj-new-plus { font-size: var(--text-callout); line-height: 1; width: 9px; text-align: center; }
</style>
