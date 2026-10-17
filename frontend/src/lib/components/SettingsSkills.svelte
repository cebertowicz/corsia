<script lang="ts">
	import { loadProjects, projects } from '$lib/stores/projects';
	import { projectAccent } from '$lib/utils/projectColors';
	import SettingsGroup from './SettingsGroup.svelte';
	import SkillScopePanel from './SkillScopePanel.svelte';

	// Settings › Skills: every level's skills on one page, the workspace first.
	let { onBrowseSkills }: { onBrowseSkills?: () => void } = $props();

	$effect(() => { void loadProjects(); });
	const active = $derived($projects.filter((project) => project.status !== 'archived'));
</script>

<SettingsGroup label="All projects" anchor="skills-workspace">
	<SkillScopePanel
		scope="workspace"
		scopeLabel="Workspace"
		heading={false}
		inheritanceHint="Every project and thread gets these. A project or thread skill with the same name replaces one."
		{onBrowseSkills}
	/>
</SettingsGroup>
{#each active as project (project.id)}
	<SettingsGroup label={project.name} mark={projectAccent(project.id, project.accent_color)} anchor={`skills-${project.id}`}>
		<SkillScopePanel
			scope="project"
			projectId={project.id}
			scopeLabel={`Project: ${project.name}`}
			heading={false}
			inheritanceHint="Threads in this project get these. A thread skill with the same name replaces one."
			{onBrowseSkills}
		/>
	</SettingsGroup>
{/each}
