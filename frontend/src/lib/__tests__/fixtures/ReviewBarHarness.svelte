<script lang="ts">
	import ReviewBar from '$lib/components/ReviewBar.svelte';

	let artifactId = $state('doc-1');

	export function switchArtifact(id: string): void {
		artifactId = id;
	}
</script>

<ReviewBar {artifactId} />
