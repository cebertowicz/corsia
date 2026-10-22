import { beforeEach, describe, expect, it, vi } from 'vitest';

const fetchArtifactPane = vi.fn();
const openArtifactAndShowThread = vi.fn();
vi.mock('$lib/api/docs', () => ({ fetchArtifactPane: (...args: unknown[]) => fetchArtifactPane(...args) }));
vi.mock('$lib/openArtifactInThread', () => ({ openArtifactAndShowThread: (...args: unknown[]) => openArtifactAndShowThread(...args) }));

import { openWaitArtifact } from './openWaitArtifact';

const node = (id: string, path: string | null) => ({ kind: 'artifact', id, path, external_id: `ext-${id}`, title: `Title ${id}` });

beforeEach(() => {
	fetchArtifactPane.mockReset();
	openArtifactAndShowThread.mockReset();
});

describe('openWaitArtifact', () => {
	it('finds the artifact by id in the thread pane and opens it by its path', async () => {
		fetchArtifactPane.mockResolvedValue({ nodes: [node('a1', '/w/a.md'), { kind: 'worktree-group', children: [node('a2', '/w/b.md')] }] });
		await openWaitArtifact('a2', 'fallback', 'thread-1');
		expect(fetchArtifactPane).toHaveBeenCalledWith('thread-1');
		expect(openArtifactAndShowThread).toHaveBeenCalledWith({ artifactId: 'a2', path: '/w/b.md', title: 'Title a2' }, 'thread-1');
	});

	it('reports the open, so the caller can say when it fails', async () => {
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		fetchArtifactPane.mockResolvedValue({ nodes: [node('a1', '/w/a.md')] });
		expect(await openWaitArtifact('a1', 'x', 'thread-1')).toBe(true);
		expect(await openWaitArtifact('missing', 'x', 'thread-1')).toBe(false);
		fetchArtifactPane.mockRejectedValue(new Error('500'));
		expect(await openWaitArtifact('a1', 'x', 'thread-1')).toBe(false);
		expect(openArtifactAndShowThread).toHaveBeenCalledTimes(1);
		expect(warn).toHaveBeenCalledTimes(2);
		warn.mockRestore();
	});
});
