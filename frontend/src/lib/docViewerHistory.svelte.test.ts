import { beforeEach, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import { createViewerHistory } from './docViewerHistory.svelte';
import { restoreVersion } from '$lib/docHistory';
import { existingDraft, flushDraft } from '$lib/stores/docSaveQueue';
import { emitProductEvent } from '$lib/product_events';

vi.mock('$lib/docHistory', () => ({ restoreVersion: vi.fn() }));
vi.mock('$lib/stores/docSaveQueue', () => ({ existingDraft: vi.fn(), flushDraft: vi.fn() }));
vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));

const target = { instanceId: 'tab', path: '/file.md', name: 'file.md', projectId: null, threadId: null, artifactId: null };
const version = { id: 'a'.repeat(40), author: 'Author', savedAt: '2026-11-03T16:02:00Z', reason: null };

function create() {
	let history!: ReturnType<typeof createViewerHistory>;
	const dispose = $effect.root(() => {
		history = createViewerHistory({ key: () => 'tab', target: () => target, closeComments: vi.fn(), root: () => undefined });
	});
	flushSync();
	return { history, dispose };
}

beforeEach(() => {
	vi.resetAllMocks();
	vi.mocked(flushDraft).mockResolvedValue(true);
	vi.mocked(existingDraft).mockReturnValue({ savedRevision: 'after-flush' } as ReturnType<typeof existingDraft>);
});

it('reports History opened once per opening', () => {
	const { history, dispose } = create();
	history.toggle();
	expect(history.open).toBe(true);
	expect(emitProductEvent).toHaveBeenCalledExactlyOnceWith('document', 'history_opened');
	history.toggle();
	expect(history.open).toBe(false);
	dispose();
});

it('saves pending typing first and asks the server to restore against what was saved', async () => {
	const order: string[] = [];
	vi.mocked(flushDraft).mockImplementation(async () => { order.push('flush'); return true; });
	vi.mocked(restoreVersion).mockImplementation(async () => { order.push('restore'); return { version: 'b'.repeat(40), sha256: 'new' }; });
	const { history, dispose } = create();
	await history.restore(version);
	expect(order).toEqual(['flush', 'restore']);
	expect(restoreVersion).toHaveBeenCalledExactlyOnceWith(target, version.id, 'after-flush');
	dispose();
});

it('sends nothing when pending typing could not be saved first', async () => {
	vi.mocked(flushDraft).mockResolvedValue(false);
	const { history, dispose } = create();
	await expect(history.restore(version)).rejects.toThrow();
	expect(restoreVersion).not.toHaveBeenCalled();
	dispose();
});
