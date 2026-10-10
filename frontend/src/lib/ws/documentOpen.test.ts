import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	openAgentDocumentTab: vi.fn(),
	getPresenceTracker: vi.fn(),
	emitProductEvent: vi.fn(),
}));

vi.mock('$lib/stores/artifactWorkspace', () => ({
	openAgentDocumentTab: mocks.openAgentDocumentTab,
}));
vi.mock('./presence', () => ({ getPresenceTracker: mocks.getPresenceTracker }));
vi.mock('$lib/product_events', () => ({ emitProductEvent: mocks.emitProductEvent }));

import { handleDocumentOpenRequest } from './documentOpen';

const frame = {
	type: 'document_open_request' as const,
	artifact_id: 'artifact-1',
	path: '/shared/design.md',
	thread_id: 'thread-1',
	title: 'design.md',
};

function visible(value: 'visible' | 'hidden'): void {
	Object.defineProperty(document, 'visibilityState', { configurable: true, value });
}

describe('document open request receipt guard', () => {
	beforeEach(() => {
		mocks.openAgentDocumentTab.mockReset();
		mocks.getPresenceTracker.mockReset();
		mocks.emitProductEvent.mockReset();
		visible('visible');
	});

	it('opens only for the visible page that still claims the frame thread', () => {
		mocks.getPresenceTracker.mockReturnValue({ claimed: true, claimedThreadId: 'thread-1', documentOpenCapability: { showing: null, canShow: true } });

		handleDocumentOpenRequest(frame);

		expect(mocks.openAgentDocumentTab).toHaveBeenCalledWith('thread-1', {
			artifactId: 'artifact-1', path: '/shared/design.md', title: 'design.md',
		});
	});

	it.each([
		['hidden page', 'hidden', { claimed: true, claimedThreadId: 'thread-1', documentOpenCapability: { showing: null, canShow: true } }],
		['no claim', 'visible', { claimed: false, claimedThreadId: null, documentOpenCapability: { showing: null, canShow: true } }],
		['other claimed thread', 'visible', { claimed: true, claimedThreadId: 'other-thread', documentOpenCapability: { showing: null, canShow: true } }],
	])('drops a frame for a %s', (_case, visibility, tracker) => {
		visible(visibility as 'visible' | 'hidden');
		mocks.getPresenceTracker.mockReturnValue(tracker);

		handleDocumentOpenRequest(frame);

		expect(mocks.openAgentDocumentTab).not.toHaveBeenCalled();
	});

	it('declines a stale compact receipt without mutating the current canvas', () => {
		mocks.getPresenceTracker.mockReturnValue({ claimed: true, claimedThreadId: 'thread-1', documentOpenCapability: { showing: null, canShow: false } });

		handleDocumentOpenRequest(frame);

		expect(mocks.openAgentDocumentTab).not.toHaveBeenCalled();
		expect(mocks.emitProductEvent).toHaveBeenCalledWith('document', 'open_declined_on_receipt', {
			threadId: 'thread-1', artifactId: 'artifact-1',
		});
	});
});
