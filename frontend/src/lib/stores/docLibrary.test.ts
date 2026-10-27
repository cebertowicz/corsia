import { get } from 'svelte/store';
import { afterEach, describe, expect, it } from 'vitest';
import { docActivity, docLibrary, type DocEntry } from './docLibrary';

function entry(path: string, description: string, descState: DocEntry['descState'], updatedTs: number): DocEntry {
	return {
		id: path, artifactId: `a-${path}`, sourceId: 's', path, abs: `/w/${path}`, name: path,
		description, descState, tokens: 1, updatedAt: '', updatedTs, updatedLabel: 'just now', searchKey: path
	};
}

describe('docActivity text', () => {
	afterEach(() => docLibrary.set([]));

	it('says a description is pending, shows one when present, and adds no dash without one', () => {
		docLibrary.set([
			entry('pending.md', '', 'pending', 3),
			entry('described.md', 'Weekly notes', 'ai', 2),
			entry('bare.md', '', 'edited', 1)
		]);
		expect(get(docActivity).map((a) => a.text)).toEqual([
			'pending.md — no description',
			'described.md — Weekly notes',
			'bare.md'
		]);
	});
});
