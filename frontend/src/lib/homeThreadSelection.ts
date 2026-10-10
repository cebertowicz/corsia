import { tick } from 'svelte';
import { get } from 'svelte/store';
import { activeThreadId, threads } from '$lib/stores/threads';
import { openThread } from '$lib/stores/openThread';
import { contextAttachTarget } from '$lib/stores/contextAttach';
import { clearDocPickerTarget } from '$lib/stores/docPickerTarget';
import { closeScopeSettings } from '$lib/stores/workspaceSettings';
import { paneOpen } from '$lib/stores/paneOpen';
import { beginThreadOpen, type ThreadOpenSource } from '$lib/openTimingRuntime';
import { enterThreadScope } from '$lib/stores/projects';

export type FirstRunSelectionOwner = (() => boolean) | undefined;

interface HomeThreadSelectionPorts {
	advanceIntent: () => void;
	/** `firstRun` is true when the first-run landing, not the user, opens the thread. */
	prepareChatSurface: (firstRun: boolean) => void;
	loadThreads: () => Promise<void>;
	selectChatThread: (id: string, source: ThreadOpenSource, owner: FirstRunSelectionOwner) => Promise<void> | void;
}

/** Route-local selection choreography; the route retains its intent state. */
export function createHomeThreadSelector(ports: HomeThreadSelectionPorts) {
	return (id: string, source: ThreadOpenSource = 'other', firstRunOwner: FirstRunSelectionOwner = undefined) =>
		selectHomeThread(id, source, firstRunOwner, ports);
}

export async function selectHomeThread(
	id: string,
	source: ThreadOpenSource,
	firstRunOwner: FirstRunSelectionOwner,
	ports: HomeThreadSelectionPorts,
): Promise<void> {
	const ownsSelection = () => !firstRunOwner || firstRunOwner();
	if (!ownsSelection()) return;
	if (!firstRunOwner) ports.advanceIntent();
	if (get(activeThreadId) !== id) beginThreadOpen(id, source);
	openThread.set(null);
	contextAttachTarget.set(null);
	clearDocPickerTarget();
	closeScopeSettings();
	ports.prepareChatSurface(!!firstRunOwner);
	paneOpen.set(false);
	await tick();
	if (!ownsSelection()) return;
	if (!get(threads).some((thread) => thread.id === id)) {
		await ports.loadThreads();
		if (!ownsSelection()) return;
	}
	const thread = get(threads).find((candidate) => candidate.id === id);
	if (thread) {
		if (!ownsSelection()) return;
		await enterThreadScope(thread, firstRunOwner);
		if (!ownsSelection()) return;
	}
	if (!ownsSelection()) return;
	await ports.selectChatThread(id, source, firstRunOwner);
}
