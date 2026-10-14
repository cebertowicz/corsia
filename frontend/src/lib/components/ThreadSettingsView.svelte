<script lang="ts">
	import ThreadContextPanel from './ThreadContextPanel.svelte';
	import ThreadAgentPanel from './ThreadAgentPanel.svelte';
	import { activeThread, activeThreadId } from '$lib/stores/threads';
	import { token } from '$lib/stores/auth';
	import { uiScope } from '$lib/stores/uiScope';
	import { fetchConductor, type ConductorTarget } from '$lib/utils/conductor';
	import { threadSettingsOpen } from '$lib/stores/threadSettings';
	import SkillScopePanel from './SkillScopePanel.svelte';

	let { onBrowseContextFiles, onBrowseSkills, conductor = false }: {
		onBrowseContextFiles?: () => void;
		onBrowseSkills?: () => void;
		conductor?: boolean;
	} = $props();

	let conductorTarget = $state<ConductorTarget | null>(null);
	let conductorLoading = $state(conductor);
	$effect(() => {
		if (!conductor || !$activeThreadId) return;
		let active = true;
		const threadId = $activeThreadId;
		conductorTarget = null;
		conductorLoading = true;
		fetchConductor($token, $uiScope).then((lookup) => {
			if (active) {
				// Absent and unknown both leave the settings unavailable to retry:
				// only a matching target can back this view.
				const target = lookup?.kind === 'target' ? lookup.target : null;
				conductorTarget = target?.thread_id === threadId ? target : null;
				conductorLoading = false;
			}
		});
		return () => { active = false; };
	});

	const settingsThreadId = $derived(conductor ? conductorTarget?.thread_id ?? null : $activeThread?.id ?? null);
	const projectId = $derived(conductor ? conductorTarget?.project_id ?? null : $activeThread?.project_id ?? null);
	const agentScope = $derived(projectId ? 'project' : 'workspace');
</script>

<div class="ts-scroll">
	<div class="ts">
		{#if conductor && !conductorTarget}
			<p role="status">{conductorLoading ? 'Loading conductor settings…' : 'Conductor settings unavailable. Try reopening this view.'}</p>
		{:else}
		<section class="sec">
			<div class="sec-cap">Context files</div>
			<p class="sec-hint">Files pinned to this thread.</p>
			<ThreadContextPanel
				threadId={settingsThreadId}
				hiddenThread={conductorTarget ? { id: conductorTarget.thread_id, title: conductorTarget.title ?? 'Conductor', project_id: conductorTarget.project_id ?? null, bootloader_files: conductorTarget.bootloader_files ?? null } : null}
				onHiddenThreadFilesChanged={(files) => { if (conductorTarget) conductorTarget = { ...conductorTarget, bootloader_files: files }; }}
				onBrowseExisting={onBrowseContextFiles}
			/>
		</section>

		<section class="sec">
			<div class="sec-cap">Thread skills</div>
			<p class="sec-hint">Skills local to this thread and inherited by its child threads.</p>
			<SkillScopePanel
				scope="thread"
				threadId={settingsThreadId}
				projectId={projectId}
				scopeLabel={`Thread: ${conductor ? 'Conductor' : $activeThread?.title ?? 'Current thread'}`}
				inheritanceHint="This thread inherits global, project, and ancestor-thread skills. Same-name local skills override inherited ones."
				onBrowseSkills={onBrowseSkills}
				onOpenSkill={() => threadSettingsOpen.set(false)}
			/>
		</section>

		{#if !conductor}
		<section class="sec">
			<div class="sec-cap">Agent</div>
			<p class="sec-hint">Promote this thread to a {agentScope} Agent when it has a clear responsibility.</p>
			<ThreadAgentPanel thread={$activeThread ?? null} />
		</section>
		{/if}
		{/if}
	</div>
</div>

<style>
	.ts-scroll {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding-top: var(--header-bottom);
	}

	.ts {
		width: 100%;
		max-width: 600px;
		margin: 0 auto;
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding: var(--space-4) var(--space-5) var(--space-8);
		text-align: left;
	}

	.sec {
		border: 1px solid var(--border-subtle);
		border-radius: var(--lg-card-radius);
		background: var(--bg-secondary);
		padding: var(--space-4) 18px;
		display: flex;
		flex-direction: column;
		gap: 11px;
	}

	.sec-cap {
		color: var(--text-tertiary);
		font-size: var(--text-caption);
		font-weight: var(--fw-bold);
		letter-spacing: var(--ls-cap);
		text-transform: uppercase;
	}

	.sec-hint {
		margin: 0;
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
	}

	@media (max-width: 768px) {
		.ts {
			padding-inline: var(--space-4);
		}
	}
</style>
