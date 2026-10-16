<script lang="ts">
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';
	import { uiScope, exitScope } from '$lib/stores/uiScope';
	import { openSectionFromProject, requestSettingsSection, settingsProject } from '$lib/stores/settingsNavigation';
	import { updateProject, archiveProject, restoreProject } from '$lib/stores/projects';
	import { projectColor } from '$lib/utils/projectColors';
	import { toasts } from '$lib/stores/toast';
	import { plainResponseError } from '$lib/plainError';
	import { Button } from '$lib/components/ui';
	import AccentPicker from './ui/AccentPicker.svelte';
	import ProjectRootPicker from './ProjectRootPicker.svelte';
	import ProjectRepos from './ProjectRepos.svelte';
	import ProvidersPolicyBlock from './ProvidersPolicyBlock.svelte';
	import SettingsGroup from './SettingsGroup.svelte';
	import SettingsRow from './SettingsRow.svelte';
	import type { ProjectDetail, AgentRow } from '$lib/api/projectSettingsTypes';

	// Settings › Projects › <name>: what this project can use, then what only it has.
	let { projectId }: { projectId: string } = $props();

	let project = $state<ProjectDetail | null>(null);
	let loading = $state(true);
	let name = $state('');
	let description = $state('');
	let busy = $state(false);
	let agentsLoading = $state(false);
	let agentsError = $state('');
	let agentRows = $state<AgentRow[]>([]);
	let rootPickerOpen = $state(false);

	// Only the project the person is in drives the shell's scope name and accent.
	const isCurrentScope = $derived(!!project && $uiScope?.id === project.id);

	let loadedFor = '';
	$effect(() => {
		const id = projectId;
		if (id && id !== loadedFor) {
			loadedFor = id;
			void load(id);
		}
	});

	async function load(id: string) {
		const t = get(token);
		if (!t) { loading = false; return; }
		loading = true;
		try {
			const res = await fetch(`/api/projects/${encodeURIComponent(id)}?token=${encodeURIComponent(t)}`);
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
				context_files: Array.isArray(d.context_files) ? d.context_files.map(String) : [],
			};
			name = project.name;
			description = project.description;
			void loadAgents(t, id);
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
			const res = await fetch(`/api/agents/${threadId}?token=${encodeURIComponent(t)}`, { method: 'DELETE' });
			if (!res.ok) throw new Error(await plainResponseError(res, 'Could not remove that. Try again.'));
			await loadAgents(t, project.id);
			toasts.show('Project Agent removed', 'success');
		} catch (e) {
			agentsError = e instanceof Error ? e.message : 'Could not remove project Agent';
		}
	}

	async function saveName() {
		if (!project) return;
		const next = name.trim();
		if (!next) { name = project.name; return; }
		if (next === project.name) return;
		try {
			await updateProject(project.id, { name: next });
			project.name = next;
			if (isCurrentScope) uiScope.update((s) => (s ? { ...s, name: next } : s));
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

	function paintScope(colour: string) {
		if (isCurrentScope) uiScope.update((s) => (s ? { ...s, color: colour } : s));
	}

	async function setAccent(c: string | null) {
		if (!project || project.accent_color === c) return;
		const prev = project.accent_color;
		project.accent_color = c;
		paintScope(c ?? projectColor(project.id));
		try {
			await updateProject(project.id, { accent_color: c });
		} catch (e) {
			project.accent_color = prev;
			paintScope(prev ?? projectColor(project.id));
			toasts.show(e instanceof Error ? e.message : "Couldn't set accent", 'error');
		}
	}

	$effect(() => {
		return () => {
			if (!project) return;
			paintScope(project.accent_color ?? projectColor(project.id));
		};
	});

	function displayName(path: string | null | undefined): string {
		if (!path) return '';
		const clean = path.replace(/\\/g, '/').replace(/\/$/, '');
		return clean.split('/').filter(Boolean).at(-1) ?? path;
	}

	function agentScopes(agent: AgentRow): string[] {
		return [...(agent.responsibility_paths ?? []), ...(agent.responsibility_domains ?? [])]
			.map((value) => value.trim())
			.filter(Boolean)
			.map((value) => (value.includes('/') ? displayName(value) : value));
	}

	function agentLine(agent: AgentRow): string {
		return [agent.responsibility, ...agentScopes(agent)].filter(Boolean).join(' · ');
	}

	async function patchProjectRoot(absPath: string) {
		if (!project) return;
		const t = get(token);
		if (!t) return;
		try {
			const res = await fetch(`/api/projects/${project.id}?token=${encodeURIComponent(t)}`, {
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

	async function archive() {
		if (!project || busy) return;
		busy = true;
		try {
			await archiveProject(project.id);
			toasts.show(`Archived ${project.name}`, 'success');
			if (isCurrentScope) exitScope();
			settingsProject.set(null);
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

	function openUse(section: 'skills' | 'context') {
		if (project) openSectionFromProject(section, { id: project.id, name: project.name }, `${section}-${project.id}`);
	}
</script>

{#if loading && !project}
	<p class="note">Loading…</p>
{:else if project}
	<div class="project-page">
		<h2 class="page-title">{project.name}</h2>

		<SettingsGroup label="What this project can use">
			<SettingsRow name="Skills" sub="For all projects, and for this one" chevron onOpen={() => openUse('skills')} />
			<SettingsRow name="Context" sub="For all projects, and for this one" chevron onOpen={() => openUse('context')} />
		</SettingsGroup>

		<SettingsGroup label="Identity">
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
				<textarea rows="2" bind:value={description} onblur={saveDescription} placeholder="Short description, shown in the projects list"></textarea>
			</label>
			<div class="fld">
				<span>Accent</span>
				<AccentPicker value={project.accent_color} onPick={setAccent} onPreview={paintScope} />
			</div>
		</SettingsGroup>

		<SettingsGroup label="Folder" hint="Conversations here share this project's context and Agents.">
			<SettingsRow
				name={project.path ? 'Working folder' : 'Working folder missing'}
				sub={project.path ? displayName(project.path) : 'No folder selected'}
				subTone={project.path ? 'quiet' : 'failed'}
			>
				{#snippet actions()}
					<Button size="sm" variant="ghost" onclick={() => (rootPickerOpen = true)}>{project?.path ? 'Change' : 'Choose folder'}</Button>
				{/snippet}
			</SettingsRow>
		</SettingsGroup>

		<div class="group-gap policy">
			<ProvidersPolicyBlock projectId={project.id} onSignIn={() => requestSettingsSection('providers')} />
		</div>

		<SettingsGroup label="Agents" hint="Threads that own one area of this project.">
			{#if agentsLoading}
				<p class="note">Loading project Agents…</p>
			{:else if agentsError}
				<p class="note failed">{agentsError}</p>
			{:else}
				{#each agentRows as agent (agent.thread_id)}
					<SettingsRow
						name={agent.display_name}
						sub={agentLine(agent)}
						menuItems={[{ id: 'remove', label: 'Remove as project Agent', danger: true }]}
						onMenuSelect={() => demoteAgent(agent.thread_id)}
					/>
				{:else}
					<p class="note">No project Agents yet.</p>
				{/each}
			{/if}
		</SettingsGroup>

		<div class="group-gap"><ProjectRepos projectId={project.id} /></div>

		<SettingsGroup label="Archive">
			{#if project.status === 'archived'}
				<SettingsRow name="This project is archived" sub="Hidden from the rail. Restore it to make it active again.">
					{#snippet actions()}<Button size="sm" variant="ghost" onclick={restore} disabled={busy}>Restore</Button>{/snippet}
				</SettingsRow>
			{:else}
				<SettingsRow name="Archive project" sub="Hides it from the rail and switchers. Nothing is deleted; restore it any time from ⌘K.">
					{#snippet actions()}<Button size="sm" variant="ghost" onclick={archive} disabled={busy}>Archive</Button>{/snippet}
				</SettingsRow>
			{/if}
		</SettingsGroup>
	</div>
{/if}

{#if rootPickerOpen && project}
	<ProjectRootPicker projectsRoot={project.projects_root} onSelect={patchProjectRoot} onClose={() => (rootPickerOpen = false)} />
{/if}

<style>
	.project-page { display: flex; flex-direction: column; }
	/* This page title owns the first gap; its first group should match the other titled Settings pages. */
	.project-page :global(.settings-group:first-of-type) { margin-top: 0; }
	.group-gap { margin-top: var(--space-6); }
	/* The policy block draws edge to edge; inset it to the page's row edge. */
	.policy { padding: 0 var(--space-2); }
	.note { margin: 0; padding: 0 var(--space-2); font-size: var(--text-caption); color: var(--text-tertiary); }
	.note.failed { color: var(--red-text); }
	.fld { display: flex; flex-direction: column; gap: var(--space-1); padding: var(--space-1) var(--space-2); }
	.fld > span:first-child { font-size: var(--text-footnote); color: var(--text-secondary); }
	.fld input, .fld textarea {
		font: inherit; font-size: var(--text-subhead);
		padding: var(--space-2) var(--space-3);
		border-radius: var(--radius-sm); border: 1px solid var(--border);
		background: var(--bg-primary); color: var(--text-primary); resize: vertical;
	}
	.fld input:focus, .fld textarea:focus { outline: none; border-color: var(--accent); }
</style>
