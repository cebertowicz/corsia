import { describe, expect, it } from 'vitest';
import { chatSend, visibleDocumentArtifactId } from './heldDocFeedbackScope';

const held = (...counts: [string, number][]) => ({
	open_count: counts.reduce((sum, [, count]) => sum + count, 0),
	artifacts: counts.map(([artifact_id, open_count]) => ({ artifact_id, open_count })),
});

describe('chatSend', () => {
	it('offers nothing when nothing is held', () => {
		expect(chatSend(null, null)).toBeNull();
		expect(chatSend(held(), 'doc-a')).toBeNull();
		expect(chatSend(held(['doc-a', 0]), null)).toBeNull();
	});

	it('names the count for a single held document that is not on screen', () => {
		expect(chatSend(held(['doc-a', 1]), null)).toEqual({ count: 1, label: 'Send 1 comment' });
		expect(chatSend(held(['doc-a', 3]), 'doc-b')).toEqual({ count: 3, label: 'Send 3 comments' });
	});

	it('leaves a single held document to its own visible header', () => {
		expect(chatSend(held(['doc-a', 3]), 'doc-a')).toBeNull();
	});

	it('says all when several documents hold comments, even beside a visible one', () => {
		expect(chatSend(held(['doc-a', 2], ['doc-b', 1]), null)).toEqual({ count: 3, label: 'Send all 3 comments' });
		expect(chatSend(held(['doc-a', 2], ['doc-b', 1]), 'doc-a')).toEqual({ count: 3, label: 'Send all 3 comments' });
	});

	it('decides by artifact, never by comparing counts: equal counts in different documents are different batches', () => {
		// The header shows doc-a's 2; the chat's 2 is the other document's.
		expect(chatSend(held(['doc-b', 2]), 'doc-a')).toEqual({ count: 2, label: 'Send 2 comments' });
		expect(chatSend(held(['doc-a', 2], ['doc-b', 2]), 'doc-a')).toEqual({ count: 4, label: 'Send all 4 comments' });
	});

	it('keeps 101 comments as a count, not a cap', () => {
		expect(chatSend(held(['doc-a', 101]), null)).toEqual({ count: 101, label: 'Send 101 comments' });
	});
});

describe('visibleDocumentArtifactId', () => {
	const open = { expanded: true, activeTabKind: 'document', artifactId: 'doc-a', imageOpen: false };

	it('is the active document of an expanded canvas', () => {
		expect(visibleDocumentArtifactId(open)).toBe('doc-a');
	});

	it.each([
		['a collapsed canvas', { expanded: false }],
		['another kind of tab', { activeTabKind: 'browser' }],
		['an image covering the document', { imageOpen: true }],
		['a tab with no document yet', { artifactId: null }],
	])('is nothing for %s', (_name, change) => {
		expect(visibleDocumentArtifactId({ ...open, ...change })).toBeNull();
	});
});
