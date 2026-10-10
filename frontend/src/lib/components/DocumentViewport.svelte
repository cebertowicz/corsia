<script lang="ts">
	import { viewingHeading } from '$lib/cm/viewing-heading';
	import { registerDocumentViewport } from '$lib/stores/documentViewport';

	let { instanceId, ready, root }: {
		instanceId: string | undefined;
		ready: boolean;
		root: HTMLElement | undefined;
	} = $props();

	$effect(() => {
		if (!ready || !instanceId || !root) return;
		const documentRoot = root;
		return registerDocumentViewport(instanceId, () => viewingHeading(documentRoot));
	});
</script>
