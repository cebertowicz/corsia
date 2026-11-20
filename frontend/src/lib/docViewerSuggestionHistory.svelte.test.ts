import { describe, expect, it, vi } from 'vitest';
import { createViewerSuggestionHistory } from './docViewerSuggestionHistory.svelte';

const mocks = vi.hoisted(() => ({ flush: vi.fn(), seed: vi.fn(), transition: vi.fn(), comments: vi.fn() }));
vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: mocks.flush, seedDraft: mocks.seed }));
vi.mock('$lib/stores/docComments', () => ({ loadComments: mocks.comments }));
vi.mock('$lib/api/commentSuggestions', () => ({ transitionSuggestionHistory: mocks.transition }));
vi.mock('$lib/stores/toast', () => ({ toasts: { show: vi.fn() } }));

const target = { instanceId: 'tab', path: '/doc.md', name: 'doc.md', projectId: 'project', threadId: 'thread', artifactId: 'artifact' };
const entry = { eventId: 'event', ids: ['s1'], direction: 'undo' as const };

describe('viewer suggestion history', () => {
	it('holds suggestion controls until the server result and comments have settled', async () => {
		let finish!: () => void;
		mocks.flush.mockResolvedValue(true);
		mocks.transition.mockResolvedValue({ text: 'old', revision: 'revision' });
		mocks.comments.mockImplementation(() => new Promise<void>(resolve => { finish = resolve; }));
		const setBusy = vi.fn();
		const history = createViewerSuggestionHistory({ target: () => target, artifact: () => 'artifact', unavailable: () => false, isLive: () => false, accept: vi.fn(), setText: vi.fn(), setBusy, reload: vi.fn() });
		const result = history.change(entry, 'new', 'old');
		expect(history.busy).toBe(true);
		await vi.waitFor(() => expect(mocks.comments).toHaveBeenCalled());
		expect(history.busy).toBe(true);
		finish();
		expect(await result).toBe(true);
		expect(history.busy).toBe(false);
		expect(setBusy.mock.calls).toEqual([[true], [false]]);
		expect(mocks.seed).toHaveBeenCalledWith(target, 'old', 'revision');
	});
});
