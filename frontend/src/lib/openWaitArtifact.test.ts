import { beforeEach, describe, expect, it, vi } from 'vitest';

const findDocument = vi.fn();
const openArtifactAndShowThread = vi.fn();
vi.mock('$lib/findDocument', () => ({ findDocument: (...args: unknown[]) => findDocument(...args) }));
vi.mock('$lib/openArtifactInThread', () => ({ openArtifactAndShowThread: (...args: unknown[]) => openArtifactAndShowThread(...args) }));

import { openWaitArtifact } from './openWaitArtifact';

beforeEach(() => {
	findDocument.mockReset();
	openArtifactAndShowThread.mockReset();
});

describe('openWaitArtifact', () => {
	it('opens the resolved artifact in the source thread', async () => {
		findDocument.mockResolvedValue({ status: 'found', path: '/w/b.md', title: 'Title' });
		expect(await openWaitArtifact('a2', 'fallback', 'thread-1')).toBe(true);
		expect(findDocument).toHaveBeenCalledWith('a2');
		expect(openArtifactAndShowThread).toHaveBeenCalledWith({ artifactId: 'a2', path: '/w/b.md', title: 'Title' }, 'thread-1');
	});

	it.each(['missing', 'failed'])('reports %s without opening', async (status) => {
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		findDocument.mockResolvedValue({ status });
		expect(await openWaitArtifact('a1', 'x', 'thread-1')).toBe(false);
		expect(openArtifactAndShowThread).not.toHaveBeenCalled();
		warn.mockRestore();
	});
});
