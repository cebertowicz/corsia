<script lang="ts">
	import ArtifactDocumentImageOverlay from './ArtifactDocumentImageOverlay.svelte';
	import { tabDocumentImage } from '$lib/stores/tabViewState';
	import type { DocumentImageComments } from '$lib/docViewerImages';
	let { comments }: { comments: DocumentImageComments } = $props();
	const image = $derived(tabDocumentImage('thread', 'tab'));
</script>

{#if image}
	<ArtifactDocumentImageOverlay {image} {comments} target={{ threadId: 'thread', instanceId: 'tab', path: '/doc.md', name: 'doc.md', projectId: null, artifactId: 'art-img' }} onBack={() => {}} onPlace={() => {}} onReplyJump={() => {}} />
{/if}
