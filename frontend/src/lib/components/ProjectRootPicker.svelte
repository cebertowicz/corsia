<script lang="ts">
	import PathPicker from './ui/PathPicker.svelte';
	import { isProjectRootChoice } from '$lib/projectRoot';

	let { projectsRoot, onSelect, onClose }: {
		projectsRoot: string;
		onSelect: (path: string) => void;
		onClose: () => void;
	} = $props();
</script>

{#if projectsRoot}
	<PathPicker
		title="Set project root"
		mode="dir"
		startPath={projectsRoot}
		rootPath={projectsRoot}
		canSelect={(path) => isProjectRootChoice(projectsRoot, path)}
		{onSelect}
		{onClose}
	/>
{/if}
