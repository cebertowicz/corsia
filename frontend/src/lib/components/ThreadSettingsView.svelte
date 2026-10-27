<script lang="ts">
	import ThreadContextPanel from './ThreadContextPanel.svelte';
	import ThreadAgentPanel from './ThreadAgentPanel.svelte';
	import { activeThread, activeThreadId } from '$lib/stores/threads';
	import { token } from '$lib/stores/auth';
	import { uiScope } from '$lib/stores/uiScope';
	import { fetchScopeTarget, type CoordinationTarget } from '$lib/utils/conductor';
	import { threadSettingsOpen } from '$lib/stores/threadSettings';
	import SkillScopePanel from './SkillScopePanel.svelte';

	let { onBrowseContextFiles, onBrowseSkills, conductor = false }: {
		onBrowseContextFiles?: () => void;
		onBrowseSkills?: () => void;
		conductor?: boolean;
	} = $props();

	let scopeTarget = $state<CoordinationTarget | null>(null);
	let conductorLoading = $state(conductor);
	$effect(() => {
		if (!conductor || !$activeThreadId) return;
		let active = true;
		const threadId = $activeThreadId;
		scopeTarget = null;
		conductorLoading = true;
		fetchScopeTarget($token, $uiScope).then((lookup) => {
			if (active) {
				// Absent and unknown both leave the settings unavailable to retry:
				// only a matching target can back this view.
				const target = lookup?.kind === 'target' ? lookup.target : null;
				scopeTarget = target?.thread_id === threadId ? target : null;
				conductorLoading = false;
			}
		});
		return () => { active = false; };
	});

	const settingsThreadId = $derived(conductor ? scopeTarget?.thread_id ?? null : $activeThread?.id ?? null);
	const projectId = $derived(conductor ? scopeTarget?.project_id ?? null : $activeThread?.project_id ?? null);
	const agentScope = $derived(projectId ? 'project' : 'workspace');
	// The workspace scope's designated thread is the CORSIA agent; a project's is a conductor.
	const designatedName = $derived($uiScope ? 'Conductor' : 'CORSIA');
</script>

<div class="ts-scroll">
	<div class="ts">
		{#if conductor && !scopeTarget}
			<p role="status">{conductorLoading ? `Loading ${designatedName === 'CORSIA' ? 'CORSIA' : 'conductor'} settings…` : `${designatedName} settings unavailable. Try reopening this view.`}</p>
		{:else}
		<section class="sec">
			<div class="sec-cap">Context files</div>
			<p class="sec-hint">Files pinned to this chat.</p>
			<ThreadContextPanel
				threadId={settingsThreadId}
				hiddenThread={scopeTarget ? { id: scopeTarget.thread_id, title: scopeTarget.title ?? designatedName, project_id: scopeTarget.project_id ?? null, bootloader_files: scopeTarget.bootloader_files ?? null } : null}
				onHiddenThreadFilesChanged={(files) => { if (scopeTarget) scopeTarget = { ...scopeTarget, bootloader_files: files }; }}
				onBrowseExisting={onBrowseContextFiles}
			/>
		</section>

		<section class="sec">
			<div class="sec-cap">Chat skills</div>
			<p class="sec-hint">Skills local to this chat and inherited by its child chats.</p>
			<SkillScopePanel
				scope="thread"
				threadId={settingsThreadId}
				projectId={projectId}
				scopeLabel={conductor ? designatedName : $activeThread?.title ?? 'this chat'}
				inheritanceHint="Includes skills from all projects, this project and parent chats. A skill with the same name here takes priority."
				onBrowseSkills={onBrowseSkills}
				onOpenSkill={() => threadSettingsOpen.set(false)}
			/>
		</section>

		{#if !conductor}
		<section class="sec">
			<div class="sec-cap">Agent</div>
			<p class="sec-hint">Promote this chat to a {agentScope} agent when it has a clear responsibility.</p>
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
