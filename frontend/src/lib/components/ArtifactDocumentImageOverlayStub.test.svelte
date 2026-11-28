<script lang="ts">
	// Stands in for the comment viewer: it asks the overlay to refresh its diagram once, as the viewer does before a post.
	let { refreshDiagram }: { refreshDiagram?: () => Promise<boolean>; [key: string]: unknown } = $props();
	export function refreshCoveredArea() {}
	$effect(() => { void refreshDiagram?.(); });
</script>
