<script lang="ts">
	import ArtifactLeaf from './ArtifactLeaf.svelte';

	let mode = $state<'expanded' | 'rail'>('expanded');

	export function setMode(next: 'expanded' | 'rail') {
		mode = next;
	}
</script>

<ArtifactLeaf threadId="thread-doc-image" {mode} />
