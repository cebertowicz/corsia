<script module lang="ts">
	// The leaf and the record of which tabs it shows are used together.
	export { createPdfTabs } from '$lib/artifactPdfTabs.svelte';
</script>

<script lang="ts">
	// A workspace PDF in the artifact leaf. Closing it goes back to the email draft whose attachment chip opened it, while that
	// draft's tab is still open; only then does the viewer offer "Back to draft".
	import PdfViewer from './PdfViewer.svelte';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import type { TabLoadSignal } from '$lib/artifactTabs';
	import { setActiveTab, type ArtifactTab } from '$lib/stores/artifactWorkspace';

	let { target, onSignal, threadId, tab, tabs, closeTab }: {
		target: DocViewerTarget;
		onSignal: (signal: TabLoadSignal) => void;
		threadId: string | null;
		/** The tab showing this PDF. */
		tab: ArtifactTab | null;
		tabs: readonly ArtifactTab[];
		closeTab: (instanceId: string) => Promise<void>;
	} = $props();

	const returnTab = $derived(tab?.openedFrom ? tabs.find((t) => t.instanceId === tab.openedFrom) ?? null : null);
</script>

<PdfViewer {target} {onSignal} backToDraft={returnTab !== null} onClose={() => {
	if (!tab) return;
	// Land on the draft first: closing the active tab would otherwise pick its neighbour.
	if (returnTab && threadId) setActiveTab(threadId, returnTab.instanceId);
	void closeTab(tab.instanceId);
}} />
