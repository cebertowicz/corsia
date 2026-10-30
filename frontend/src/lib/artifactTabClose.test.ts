import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	sendDocFeedback: vi.fn(),
	closeArtifactTab: vi.fn(),
	setActiveSurface: vi.fn(),
	closeTabWithSaveGate: vi.fn(),
	returnToPlanNode: vi.fn(),
	show: vi.fn(),
}));

vi.mock('$lib/api/docFeedback', () => ({
	DocFeedbackError: class extends Error {
		constructor(readonly status: number) { super(`HTTP ${status}`); }
	},
	sendDocFeedback: mocks.sendDocFeedback,
}));
vi.mock('$lib/stores/artifactWorkspace', () => ({
	closeArtifactTab: mocks.closeArtifactTab,
	setActiveSurface: mocks.setActiveSurface,
}));
vi.mock('$lib/stores/docSaveQueue', () => ({ closeTabWithSaveGate: mocks.closeTabWithSaveGate }));
vi.mock('$lib/planLeafReturn', () => ({ returnToPlanNode: mocks.returnToPlanNode }));
vi.mock('$lib/stores/toast', () => ({ toasts: { show: mocks.show } }));

import { DocFeedbackError } from '$lib/api/docFeedback';
import { requestArtifactTabClose } from './artifactTabClose';
import type { ArtifactTab } from '$lib/artifactTabTypes';

const doc = { kind: 'document', instanceId: 'tab-1', artifactId: 'art-1', path: '/a.md', title: 'a.md' } as unknown as ArtifactTab;

beforeEach(() => {
	for (const mock of Object.values(mocks)) mock.mockReset();
	mocks.closeTabWithSaveGate.mockResolvedValue({ closed: true });
	mocks.sendDocFeedback.mockResolvedValue({ woken: [], undelivered_batches: [], open_count: 0, document_held_count: 0, image_held_count: 0 });
});

describe('requestArtifactTabClose', () => {
	it('sends the open batch after a clean save, then closes the tab', async () => {
		await requestArtifactTabClose('t1', doc, vi.fn());
		expect(mocks.closeTabWithSaveGate).toHaveBeenCalledWith({ instanceId: 'tab-1' }, expect.any(Object));
		expect(mocks.sendDocFeedback).toHaveBeenCalledWith('art-1');
		expect(mocks.sendDocFeedback.mock.invocationCallOrder[0]).toBeLessThan(mocks.closeArtifactTab.mock.invocationCallOrder[0]);
		expect(mocks.closeArtifactTab).toHaveBeenCalledWith('t1', 'tab-1');
	});

	it('neither sends nor closes when the pending save fails', async () => {
		mocks.closeTabWithSaveGate.mockResolvedValue({ closed: false });
		await requestArtifactTabClose('t1', doc, vi.fn());
		expect(mocks.sendDocFeedback).not.toHaveBeenCalled();
		expect(mocks.closeArtifactTab).not.toHaveBeenCalled();
	});

	it('closes and says how to recover when the send fails', async () => {
		mocks.sendDocFeedback.mockRejectedValue(new TypeError('Failed to fetch'));
		await requestArtifactTabClose('t1', doc, vi.fn());
		expect(mocks.closeArtifactTab).toHaveBeenCalledWith('t1', 'tab-1');
		expect(mocks.show).toHaveBeenCalledWith("Couldn't send your comments. Reopen the document to send them.", 'error');
	});

	it('closes silently when the artifact is gone', async () => {
		mocks.sendDocFeedback.mockRejectedValue(new DocFeedbackError(404));
		await requestArtifactTabClose('t1', doc, vi.fn());
		expect(mocks.closeArtifactTab).toHaveBeenCalledWith('t1', 'tab-1');
		expect(mocks.show).not.toHaveBeenCalled();
	});

	it('closes a subagent tab without a save gate or send and returns to its plan node', async () => {
		const subagent = { kind: 'subagent', instanceId: 'tab-2', threadId: 'child' } as unknown as ArtifactTab;
		await requestArtifactTabClose('t1', subagent, vi.fn());
		expect(mocks.closeTabWithSaveGate).not.toHaveBeenCalled();
		expect(mocks.sendDocFeedback).not.toHaveBeenCalled();
		expect(mocks.closeArtifactTab).toHaveBeenCalledWith('t1', 'tab-2');
		expect(mocks.setActiveSurface).toHaveBeenCalledWith('t1', 'chat');
		expect(mocks.returnToPlanNode).toHaveBeenCalledWith(subagent);
	});
});
