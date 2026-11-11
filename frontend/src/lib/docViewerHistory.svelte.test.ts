import { beforeEach, expect, it, vi } from 'vitest';
import { flushSync, tick } from 'svelte';
import { createViewerHistory } from './docViewerHistory.svelte';
import { restoreVersion } from '$lib/docHistory';
import { existingDraft, flushDraft } from '$lib/stores/docSaveQueue';
import { emitProductEvent } from '$lib/product_events';

vi.mock('$lib/docHistory', () => ({ restoreVersion: vi.fn() }));
vi.mock('$lib/stores/docSaveQueue', () => ({ existingDraft: vi.fn(), flushDraft: vi.fn() }));
vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));

const target = { instanceId: 'tab', path: '/file.md', name: 'file.md', projectId: null, threadId: null, artifactId: null };
const version = { id: 'a'.repeat(40), author: 'Author', savedAt: '2026-11-03T16:02:00Z', reason: null };

function create(liveText?: () => string | null) {
	let history!: ReturnType<typeof createViewerHistory>;
	const dispose = $effect.root(() => {
		history = createViewerHistory({ key: () => 'tab', target: () => target, closeComments: vi.fn(), columnShown: () => false, showColumn: vi.fn(), root: () => undefined, liveText });
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

it('offers History only for the tab whose open said it has history, and mirrors a pending restore', () => {
	let key = $state('tab');
	let history!: ReturnType<typeof createViewerHistory>;
	const dispose = $effect.root(() => {
		history = createViewerHistory({ key: () => key, target: () => target, closeComments: vi.fn(), columnShown: () => false, showColumn: vi.fn(), root: () => undefined });
	});
	flushSync();
	expect(history.available).toBe(false);
	history.setAvailable(true);
	history.setRestoring(true);
	expect(history.available).toBe(true);
	expect(history.restoring).toBe(true);
	history.setRestoring(false);
	key = 'other-tab';
	flushSync();
	expect(history.available).toBe(false);
	expect(history.restoring).toBe(false);
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

it('restores an open live document against the full SHA-256 of the text it shows', async () => {
	const shown = 'Live text.\n';
	const { history, dispose } = create(() => shown);
	await history.restore(version);
	const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(shown));
	const hex = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
	expect(restoreVersion).toHaveBeenCalledExactlyOnceWith(target, version.id, hex);
	dispose();
});

it.each([
	['refused', () => vi.mocked(flushDraft).mockResolvedValue(false)],
	['offline', () => vi.mocked(flushDraft).mockRejectedValue(new TypeError('offline'))],
])('sends nothing and names the cause when pending typing could not be saved first (%s)', async (_case, arrange) => {
	arrange();
	const { history, dispose } = create();
	await expect(history.restore(version)).rejects.toMatchObject({ kind: 'unsaved_typing' });
	expect(restoreVersion).not.toHaveBeenCalled();
	dispose();
});

it('names a missing saved revision as a document still loading', async () => {
	vi.mocked(existingDraft).mockReturnValue(undefined);
	const { history, dispose } = create();
	await expect(history.restore(version)).rejects.toMatchObject({ kind: 'not_loaded' });
	expect(restoreVersion).not.toHaveBeenCalled();
	dispose();
});

function createWithColumn(initial: { shown: boolean }) {
	const showColumn = vi.fn();
	let history!: ReturnType<typeof createViewerHistory>;
	const dispose = $effect.root(() => {
		history = createViewerHistory({ key: () => 'tab', target: () => target, closeComments: vi.fn(() => { initial.shown = false; }), columnShown: () => initial.shown, showColumn, root: () => undefined });
	});
	flushSync();
	return { history, dispose, showColumn };
}

it('brings the margin column back when History closes, if it was up when History opened', async () => {
	const column = { shown: true };
	const { history, dispose, showColumn } = createWithColumn(column);
	history.toggle();
	expect(history.open).toBe(true);
	await tick();
	expect(showColumn).not.toHaveBeenCalled();
	history.toggle();
	expect(history.open).toBe(false);
	expect(showColumn).not.toHaveBeenCalled();
	await tick();
	expect(showColumn).toHaveBeenCalledTimes(1);
	dispose();
});

it('brings the column back when History is closed from its own Close button too, and only once', async () => {
	const { history, dispose, showColumn } = createWithColumn({ shown: true });
	history.toggle();
	history.close();
	history.close();
	await tick();
	expect(showColumn).toHaveBeenCalledTimes(1);
	dispose();
});

it('leaves the column closed when it was not up before History opened', async () => {
	const { history, dispose, showColumn } = createWithColumn({ shown: false });
	history.toggle();
	history.toggle();
	await tick();
	expect(showColumn).not.toHaveBeenCalled();
	dispose();
});

it('does not remember a column from an earlier opening', async () => {
	const column = { shown: true };
	const { history, dispose, showColumn } = createWithColumn(column);
	history.toggle(); history.toggle();
	await tick();
	showColumn.mockClear();
	column.shown = false;
	history.toggle(); history.toggle();
	await tick();
	expect(showColumn).not.toHaveBeenCalled();
	dispose();
});
