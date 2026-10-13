import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import {
	closeArtifactTab, openArtifactTab, recordTabDocumentPlace, tabViewState, threadWorkspaces,
} from './stores/artifactWorkspace';

const THREAD = 'tab-place-thread';

function open(path: string): string {
	openArtifactTab(THREAD, { path, title: path });
	return get(threadWorkspaces)[THREAD].activeTabId!;
}

describe('tab view place ownership', () => {
	beforeEach(() => {
		localStorage.clear();
		threadWorkspaces.set({});
	});

	it('keeps text anchors with their tab, never lets an incoming tab replace the outgoing one, and drops them on close', () => {
		const outgoing = open('/outgoing.md');
		const incoming = open('/incoming.md');
		const outgoingPlace = { from: 240, top: -8, scrollTop: 1548 };

		recordTabDocumentPlace(THREAD, outgoing, outgoingPlace);
		recordTabDocumentPlace(THREAD, incoming, { from: 0, top: 0, scrollTop: 0 });

		expect(tabViewState(THREAD, outgoing)?.documentPlace).toEqual(outgoingPlace);
		expect(tabViewState(THREAD, incoming)?.documentPlace).toEqual({ from: 0, top: 0, scrollTop: 0 });

		closeArtifactTab(THREAD, outgoing);
		expect(tabViewState(THREAD, outgoing)).toBeNull();
	});
});
