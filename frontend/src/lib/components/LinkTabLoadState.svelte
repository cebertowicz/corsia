<script lang="ts">
	// A pressed link's tab before it names a file: the document's own loading state, then its failure with Retry.
	import DocLoadState from './DocLoadState.svelte';
	import { retryLinkTab } from '$lib/linkTargetResolver';
	import type { ArtifactLink } from '$lib/artifactTabTypes';

	let { threadId, instanceId, link, title, failed }: {
		threadId: string; instanceId: string; link: ArtifactLink; title: string; failed: boolean;
	} = $props();
</script>

<div class="doc"><div class="doc-main">
	<DocLoadState state={failed ? 'error' : 'loading'} name={title} onRetry={() => void retryLinkTab(threadId, instanceId, link)} />
</div></div>
