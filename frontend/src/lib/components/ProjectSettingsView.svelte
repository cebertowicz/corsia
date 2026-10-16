<script lang="ts">
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';
	import { uiScope, exitScope } from '$lib/stores/uiScope';
	import { projectSettingsOpen } from '$lib/stores/projectSettings';
	import { updateProject, archiveProject, restoreProject } from '$lib/stores/projects';
	import { projectColor } from '$lib/utils/projectColors';
	import { toasts } from '$lib/stores/toast';
	import { plainResponseError } from '$lib/plainError';
	import AccentPicker from './ui/AccentPicker.svelte';
	import ProjectRootPicker from './ProjectRootPicker.svelte';
	import ProjectRepos from './ProjectRepos.svelte';
	import ProvidersPolicyBlock from './ProvidersPolicyBlock.svelte';
	import SkillScopePanel from './SkillScopePanel.svelte';
	let { onBrowseContextFiles, onBrowseSkills, onOpenProviders }: {
		onBrowseContextFiles?: () => void;
		onBrowseSkills?: () => void;
		onOpenProviders?: () => void;
	} = $props();
	import type { ProjectDetail, AgentRow } from '$lib/api/projectSettingsTypes';
	let project = $state<ProjectDetail | null>(null);
	let loading = $state(true);
	let name = $state('');
	let description = $state('');
	let busy = $state(false);
	let agentsLoading = $state(false);
	let agentsError = $state('');
	let agentRows = $state<AgentRow[]>([]);

	let loadedFor = '';
	$effect(() => {
		const id = $uiScope?.id ?? '';
		if (id && id !== loadedFor) {
			loadedFor = id;
			load(id);
		}
	});

	async function load(id: string) {
		const t = get(token);
		if (!t) { loading = false; return; }
		loading = true;
		try {
			const res = await fetch(`/api/projects/${id}?token=${t}`);
			if (!res.ok) throw new Error(`Couldn't load project (${res.status})`);
			const d = await res.json();
			project = {
				id: d.id,
				name: d.name,
				description: d.description ?? '',
				status: d.status,
				accent_color: d.accent_color ?? null,
				path: d.path ?? null,
				projects_root: typeof d.projects_root === 'string' ? d.projects_root : '',
				context_files: Array.isArray(d.context_files)
					? d.context_files.map(String)
					: [],
			};
			name = project.name;
			description = project.description;
			loadAgents(t, id);
		} catch (e) {
			toasts.show(e instanceof Error ? e.message : 'Could not load project', 'error');
		} finally {
			loading = false;
		}
	}

	async function loadAgents(t: string, id: string) {
		agentsLoading = true;
		agentsError = '';
		try {
			const res = await fetch(`/api/agents?token=${encodeURIComponent(t)}&project_id=${encodeURIComponent(id)}`);
			if (!res.ok) throw new Error(`Couldn't load project Agents (${res.status})`);
			const body = await res.json();
			agentRows = Array.isArray(body.agents) ? body.agents : [];
		} catch (e) {
			agentsError = e instanceof Error ? e.message : 'Could not load project Agents';
		} finally {
			agentsLoading = false;
		}
	}

	async function demoteAgent(threadId: string) {
		const t = get(token);
		if (!t || !project) return;
		try {
			const res = await fetch(`/api/agents/${threadId}?token=${encodeURIComponent(t)}`, {
				method: 'DELETE',
			});
			if (!res.ok) throw new Error(await plainResponseError(res, 'Could not remove that. Try again.'));
			await loadAgents(t, project.id);
			toasts.show('Project Agent removed', 'success');
		} catch (e) {
			agentsError = e instanceof Error ? e.message : 'Could not remove project Agent';
		}
	}

	function close() { projectSettingsOpen.set(false); }

	async function saveName() {
		if (!project) return;
		const next = name.trim();
		if (!next) { name = project.name; return; }
		if (next === project.name) return;
		try {
			await updateProject(project.id, { name: next });
			project.name = next;
			uiScope.update((s) => (s ? { ...s, name: next } : s));
			toasts.show('Saved', 'success');
		} catch (e) {
			toasts.show(e instanceof Error ? e.message : "Couldn't rename project", 'error');
			name = project.name;
		}
	}

	async function saveDescription() {
		if (!project) return;
		const next = description.trim();
		if (next === project.description) return;
		try {
			await updateProject(project.id, { description: next });
			project.description = next;
		} catch (e) {
			toasts.show(e instanceof Error ? e.message : "Couldn't save description", 'error');
			description = project.description;
		}
	}

	async function setAccent(c: string | null) {
		if (!project || project.accent_color === c) return;
		const prev = project.accent_color;
		const resolved = c ?? projectColor(project.id);
		project.accent_color = c;
		uiScope.update((s) => (s ? { ...s, color: resolved } : s));
		try {
			await updateProject(project.id, { accent_color: c });
		} catch (e) {
			project.accent_color = prev;
			uiScope.update((s) => (s ? { ...s, color: prev ?? projectColor(project!.id) } : s));
			toasts.show(e instanceof Error ? e.message : "Couldn't set accent", 'error');
		}
	}

	function previewAccent(c: string) {
		uiScope.update((s) => (s ? { ...s, color: c } : s));
	}

	$effect(() => {
		return () => {
			if (!project) return;
			const ground = project.accent_color ?? projectColor(project.id);
			uiScope.update((s) => (s ? { ...s, color: ground } : s));
		};
	});

	const bootFiles = $derived(project?.context_files ?? []);
	let rootPickerOpen = $state(false);
	let newBootloaderPath = $state('');

	function displayName(path: string | null | undefined): string {
		if (!path) return '';
		const clean = path.replace(/\\/g, '/').replace(/\/$/, '');
		const parts = clean.split('/').filter(Boolean);
		return parts.at(-1) ?? path;
	}

	function agentScopeLabel(value: string): string {
		const trimmed = value.trim();
		if (!trimmed) return '';
		if (trimmed.includes('/')) return displayName(trimmed);
		return trimmed;
	}

	function agentScopes(agent: AgentRow): string[] {
		return [...(agent.responsibility_paths ?? []), ...(agent.responsibility_domains ?? [])]
			.map(agentScopeLabel)
			.filter(Boolean);
	}

	async function patchProjectRoot(absPath: string) {
		if (!project) return;
		const t = get(token);
		if (!t) return;
		try {
			const res = await fetch(`/api/projects/${project.id}?token=${t}`, {
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ path: absPath }),
			});
			if (!res.ok) throw new Error(await plainResponseError(res, 'Could not save these settings. Try again.'));
			const body = await res.json();
			project.path = body.project?.path ?? absPath;
			toasts.show('Saved', 'success');
		} catch (e) {
			toasts.show(e instanceof Error ? e.message : "Couldn't update project root", 'error');
		}
	}

	async function createBootloaderFile() {
		if (!project || !newBootloaderPath.trim()) return;
		const t = get(token);
		if (!t) return;
		try {
			const res = await fetch('/api/context/bootloader/file', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${t}` },
				body: JSON.stringify({
					scope: 'project',
					project_id: project.id,
					path: newBootloaderPath.trim(),
					content: `# ${newBootloaderPath.trim()}\n`,
				}),
			});
			if (!res.ok) throw new Error(await plainResponseError(res, 'Could not create that. Try again.'));
			const body = await res.json();
			project.context_files = Array.isArray(body.files) ? body.files : bootFiles;
			newBootloaderPath = '';
			toasts.show('Created', 'success');
		} catch (e) {
			toasts.show(e instanceof Error ? e.message : "Couldn't create bootloader file", 'error');
		}
	}

	async function archive() {
		if (!project || busy) return;
		busy = true;
		try {
			await archiveProject(project.id);
			toasts.show(`Archived ${project.name}`, 'success');
			exitScope();
			close();
		} catch (e) {
			toasts.show(e instanceof Error ? e.message : "Couldn't archive project", 'error');
		} finally {
			busy = false;
		}
	}

	async function restore() {
		if (!project || busy) return;
		busy = true;
		try {
			await restoreProject(project.id);
			project.status = 'active';
			toasts.show(`Restored ${project.name}`, 'success');
		} catch (e) {
			toasts.show(e instanceof Error ? e.message : "Couldn't restore project", 'error');
		} finally {
			busy = false;
		}
	}
</script>

<div class="ps-scroll">
	<div class="ps">
	{#if loading && !project}
		<div class="ps-loading">Loading…</div>
	{:else if project}
		<section class="sec">
			<div class="sec-cap">Identity</div>

			<label class="fld">
				<span>Name</span>
				<input
					type="text"
					bind:value={name}
					onblur={saveName}
					onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); (e.target as HTMLInputElement).blur(); } }}
				/>
			</label>

			<label class="fld">
				<span>Description</span>
				<textarea
					rows="2"
					bind:value={description}
					onblur={saveDescription}
					placeholder="Short description — shown in the projects list…"
				></textarea>
			</label>

			<div class="fld">
				<span>Accent</span>
				<AccentPicker value={project.accent_color} onPick={setAccent} onPreview={previewAccent} />
			</div>
		</section>

		<section class="sec">
			<div class="sec-cap">Scope and roots</div>
			<div class="row scope-row">
				<div class="row-text">
					<span class="row-title">Project boundary</span>
					<span class="row-sub">Threads here share project context and project Agents.</span>
				</div>
			</div>
			<div class="row scope-row">
				<div class="row-text">
					<span class="row-title">{project.path ? 'Working root' : 'Working root missing'}</span>
					<span class="row-sub scope-path">{project.path ? displayName(project.path) : 'No directory selected'}</span>
				</div>
				<button class="btn repair-root" onclick={() => (rootPickerOpen = true)}>
					{project.path ? 'Change root' : 'Repair root'}
				</button>
			</div>
		</section>

		<ProvidersPolicyBlock projectId={project.id} onSignIn={onOpenProviders} />

		<section class="sec">
			<div class="sec-cap">Project context</div>
			<p class="sec-hint">Markdown files in context/ are loaded for every conversation in this project.</p>
			{#if bootFiles.length === 0}
				<div class="sec-hint">No project context files.</div>
			{/if}
			{#each bootFiles as f (f)}
				<div class="row boot">
					<svg class="boot-ico" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
					<span class="row-title">{displayName(f)}</span>
				</div>
			{/each}
			<details class="advanced-create">
				<summary>Create new context file</summary>
				<div class="boot-create-row">
					<input
						bind:value={newBootloaderPath}
						placeholder="File name (.md)"
						aria-label="New project context file"
						onkeydown={(e) => { if (e.key === 'Enter') void createBootloaderFile(); }}
					/>
					<button class="add create-bootloader-file" disabled={!newBootloaderPath.trim()} onclick={createBootloaderFile}>Create</button>
				</div>
			</details>
		</section>

		<section class="sec">
			<div class="sec-cap">Project skills</div>
			<p class="sec-hint">Skills available to every thread in this project unless a thread overrides them.</p>
			<SkillScopePanel
				scope="project"
				projectId={project.id}
				scopeLabel={`Project: ${project.name}`}
				inheritanceHint="Inherited by threads in this project. Thread skills with the same name override these."
				onBrowseSkills={onBrowseSkills}
				onOpenSkill={close}
			/>
		</section>

		<section class="sec">
			<div class="sec-cap">Project Agents</div>
			<p class="sec-hint">Threads with clear responsibility for a project area.</p>
			{#if agentsLoading}
				<div class="sec-hint">Loading project Agents...</div>
			{:else if agentsError}
				<div class="sec-hint">{agentsError}</div>
			{:else if agentRows.length === 0}
				<div class="sec-hint">No project Agents yet.</div>
			{:else}
				{#each agentRows as agent (agent.thread_id)}
					<div class="row agent-row">
						<div class="row-text">
							<span class="row-title">{agent.display_name}</span>
							<span class="row-sub">{agent.responsibility}</span>
							{#if agent.responsibility_paths?.length || agent.responsibility_domains?.length}
								<div class="scope-chips">
									{#each agentScopes(agent) as scope}
										<span>{scope}</span>
									{/each}
								</div>
							{/if}
						</div>
						<button
							class="iconbtn"
							title="Remove project Agent"
							aria-label="Remove project Agent {agent.display_name}"
							onclick={() => demoteAgent(agent.thread_id)}
						>✕</button>
					</div>
				{/each}
			{/if}
		</section>

		<ProjectRepos projectId={project.id} />

		<section class="sec lifecycle">
			<div class="sec-cap">Lifecycle</div>
			{#if project.status === 'archived'}
				<div class="row">
					<div class="row-text">
						<span class="row-title">This project is archived</span>
						<span class="row-sub">Hidden from the rail. Restore it to make it active again.</span>
					</div>
					<button class="btn" onclick={restore} disabled={busy}>Restore</button>
				</div>
			{:else}
				<div class="row">
					<div class="row-text">
						<span class="row-title">Archive project</span>
						<span class="row-sub">Hide it from the rail and switchers. Nothing is deleted — restore any time from ⌘K.</span>
					</div>
					<button class="btn warn" onclick={archive} disabled={busy}>Archive</button>
				</div>
			{/if}
		</section>
	{/if}
	</div>
</div>

{#if rootPickerOpen && project}
	<ProjectRootPicker
		projectsRoot={project.projects_root}
		onSelect={patchProjectRoot}
		onClose={() => (rootPickerOpen = false)}
	/>
{/if}

<style>
	.ps-scroll {
		flex: 1; min-height: 0; overflow-y: auto;
		padding-top: var(--header-bottom);
	}
	.ps {
		width: 100%; max-width: 600px; margin: 0 auto;
		display: flex; flex-direction: column; gap: 12px;
		padding: 16px 20px 40px; text-align: left;
	}
	.ps-loading { padding: 32px; text-align: center; color: var(--text-tertiary); font-size: var(--text-subhead); }

	.sec {
		border: 1px solid var(--border-subtle); border-radius: var(--lg-card-radius);
		background: var(--bg-secondary); padding: 16px 18px;
		display: flex; flex-direction: column; gap: 11px;
	}
	.sec-cap {
		font-size: var(--text-caption); font-weight: 700; text-transform: uppercase;
		letter-spacing: 0.1em; color: var(--text-tertiary);
	}

	.fld { display: flex; flex-direction: column; gap: 6px; }
	.fld > span:first-child { font-size: var(--text-footnote); color: var(--text-secondary); }
	.fld input, .fld textarea {
		font: inherit; font-size: var(--text-subhead); padding: 9px 11px;
		border-radius: var(--radius-sm); border: 1px solid var(--border);
		background: var(--bg-primary); color: var(--text-primary); resize: vertical;
	}
	.fld input:focus, .fld textarea:focus { outline: none; border-color: var(--accent); }

	.row { display: flex; align-items: center; gap: 14px; }
	.scope-row { align-items: flex-start; }
	.scope-path {
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.row.boot { gap: 11px; padding: 2px 0; }
	.row.boot .row-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.boot-ico { color: var(--text-tertiary); flex-shrink: 0; }
	.sec-hint { font-size: var(--text-footnote); color: var(--text-tertiary); margin: 0; }

	.iconbtn {
		background: none; border: none; cursor: pointer; color: var(--text-tertiary);
		font-size: var(--text-footnote); padding: 3px 6px; border-radius: var(--radius-xs); flex-shrink: 0; line-height: 1;
	}
	.iconbtn:hover { background: color-mix(in srgb, var(--red) 10%, transparent); color: var(--red); }

	.add {
		align-self: flex-start; background: none; border: 1px dashed var(--border);
		border-radius: var(--radius-sm); cursor: pointer; color: var(--text-secondary);
		font-size: var(--text-subhead); padding: 6px 12px; font-family: inherit;
	}
	.add:hover { color: var(--violet-text); border-color: var(--accent); }
	.add:disabled { opacity: 0.45; cursor: not-allowed; }
	.boot-create-row {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 8px;
		margin-top: 8px;
	}
	.boot-create-row input {
		min-width: 0;
		font: inherit;
		font-size: var(--text-subhead);
		padding: 7px 10px;
		border-radius: var(--radius-sm);
		border: 1px solid var(--border);
		background: var(--bg-primary);
		color: var(--text-primary);
	}
	.row-text { display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: 0; }
	.row-title { font-size: var(--text-subhead); color: var(--text-primary); }
	.row-sub { font-size: var(--text-footnote); color: var(--text-tertiary); }
	.agent-row { align-items: flex-start; }
	.scope-chips {
		display: flex;
		flex-wrap: wrap;
		gap: 5px;
		margin-top: 4px;
	}
	.scope-chips span {
		max-width: 180px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-xs);
		color: var(--text-secondary);
		font-size: var(--text-caption);
		padding: 2px 7px;
	}
	.advanced-create {
		border-top: 1px solid var(--border-subtle);
		padding-top: 8px;
	}
	.advanced-create summary {
		cursor: pointer;
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
	}
	.btn {
		font: inherit; font-size: var(--text-subhead); font-weight: 500;
		padding: 7px 14px; border-radius: var(--radius-sm); cursor: pointer;
		background: none; border: 1px solid var(--border); color: var(--text-secondary);
		flex-shrink: 0;
	}
	.btn:hover:not(:disabled) { color: var(--text-primary); background: var(--bg-hover); }
	.btn:disabled { opacity: 0.5; cursor: not-allowed; }
	.btn.warn { color: var(--amber, #d97706); border-color: color-mix(in srgb, var(--amber, #d97706) 45%, transparent); }
	.btn.warn:hover:not(:disabled) { background: color-mix(in srgb, var(--amber, #d97706) 10%, transparent); color: var(--amber, #d97706); }
</style>
