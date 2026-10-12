import { afterEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { documentImageViewers, openDocumentImage, remapDocumentImage } from './documentImageViewer';

const image = (instanceId: string) => ({ instanceId, src: '/img', alt: 'Mock', from: 9, to: 23, scrollTop: 0, top: 0 });

afterEach(() => documentImageViewers.set({}));

describe('an open image of a document', () => {
	it('follows its markdown through an edit of the document it was opened from', () => {
		openDocumentImage('t', image('tab-1'));
		remapDocumentImage('t', 'tab-1', (pos) => pos + 4);
		expect(get(documentImageViewers).t).toMatchObject({ from: 13, to: 27 });
	});

	it('ignores an edit of another tab, and changes nothing when the edit is elsewhere', () => {
		openDocumentImage('t', image('tab-1'));
		const before = get(documentImageViewers);
		remapDocumentImage('t', 'tab-2', (pos) => pos + 4);
		remapDocumentImage('t', 'tab-1', (pos) => pos);
		remapDocumentImage('other', 'tab-1', (pos) => pos + 4);
		expect(get(documentImageViewers)).toBe(before);
	});
});
