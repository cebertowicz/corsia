<script lang="ts">
	import { loadProjects, projectLoadStatus, projects } from '$lib/stores/projects';
	import { openSettingsProject, settingsNavigation } from '$lib/stores/settingsNavigation';
	import { projectAccent } from '$lib/utils/projectColors';
	import ProjectSettingsView from './ProjectSettingsView.svelte';
	import SettingsGroup from './SettingsGroup.svelte';
	import SettingsRow from './SettingsRow.svelte';

	// Settings › Projects: every project, and one project's page once picked.
	$effect(() => { void loadProjects(); });
	const openProject = $derived($settingsNavigation.project);
	const active = $derived($projects.filter((project) => project.status !== 'archived'));
	const archived = $derived($projects.filter((project) => project.status === 'archived'));
</script>

{#if openProject}
	{#key openProject}<ProjectSettingsView projectId={openProject} />{/key}
{:else}
	<h2 class="page-title">Projects</h2>
	{#if $projectLoadStatus === 'error'}
		<p class="note failed">Couldn't load projects.</p>
	{:else if $projectLoadStatus === 'loaded' && $projects.length === 0}
		<p class="note">No projects yet.</p>
	{/if}
	{#if active.length}
		<SettingsGroup label="Active">
			{#each active as project (project.id)}
				<SettingsRow name={project.name} mark={projectAccent(project.id, project.accent_color)} chevron onOpen={() => openSettingsProject(project.id)} />
			{/each}
		</SettingsGroup>
	{/if}
	{#if archived.length}
		<SettingsGroup label="Archived">
			{#each archived as project (project.id)}
				<SettingsRow name={project.name} mark={projectAccent(project.id, project.accent_color)} chevron onOpen={() => openSettingsProject(project.id)} />
			{/each}
		</SettingsGroup>
	{/if}
{/if}

<style>
	.note { margin: 0; padding: 0 var(--space-2); font-size: var(--text-caption); color: var(--text-tertiary); }
	.note.failed { color: var(--red-text); }
</style>
