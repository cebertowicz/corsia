import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { activeThreadId, activeToolsByThread, messages } from '$lib/stores/threads';
import { token } from '$lib/stores/auth';
import { coreSettings } from '$lib/stores/settings';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { _resetCheckpointsForTests } from '$lib/stores/checkpoints';
import MessageList from './MessageList.svelte';

const THREAD_ID = 'artifact-removal-session';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeAll(() => {
	if (!Element.prototype.animate) {
		Element.prototype.animate = (() => ({
			finished: Promise.resolve(),
			cancel: () => {},
			commitStyles: () => {},
			onfinish: null,
			playState: 'finished',
		})) as unknown as Element['animate'];
	}
});

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set(THREAD_ID);
	_resetTurnStateForTests();
	_resetCheckpointsForTests();
	activeToolsByThread.set(new Map());
	coreSettings.set({ features: { focus_mode: false } } as any);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	messages.set([]);
	token.set(null);
	coreSettings.set(null);
	_resetCheckpointsForTests();
});

function renderList() {
	component = mount(MessageList, {
		target: host,
		props: {
			messagesEl: undefined,
			userIsNearBottom: true,
			ignoreScrollEvents: false,
			onOpenLightbox: () => {},
		},
	});
	flushSync();
}

describe('MessageList artifact removal rendering', () => {
	it('renders a single removal event with the subtle quiet treatment', () => {
		messages.set([{
			id: 'removal-1',
			thread_id: THREAD_ID,
			role: 'system',
			content: 'Removed design.md',
			created_at: '2026-08-15T00:00:00Z',
			metadata: {
				type: 'artifact_removed',
				event_type: 'artifact.hidden_from_pane',
				subtype: 'hidden_from_pane',
				artifact_id: 'art-1',
				artifact_type: 'document',
				title: 'design.md',
				actor_kind: 'user',
				causal_event_id: null,
				hidden: true,
			},
		}]);
		renderList();

		expect(host.textContent).toContain('Removed design.md');
		expect(host.querySelector('.message-content.context-event')?.tagName).toBe('DIV');
	});

	it('collapses a same-turn batch into one concise grouped line', () => {
		messages.set([
			{
				id: 'removal-1',
				thread_id: THREAD_ID,
				role: 'system',
				content: 'Released alpha.md',
				created_at: '2026-08-15T00:00:00Z',
				metadata: {
					type: 'artifact_removed',
					event_type: 'artifact.ownership_released',
					subtype: 'ownership_released',
					artifact_id: 'art-1',
					artifact_type: 'document',
					title: 'alpha.md',
					actor_kind: 'agent',
					causal_event_id: 'turn-batch-1',
				},
			},
			{
				id: 'removal-2',
				thread_id: THREAD_ID,
				role: 'system',
				content: 'Released beta.md',
				created_at: '2026-08-15T00:00:01Z',
				metadata: {
					type: 'artifact_removed',
					event_type: 'artifact.ownership_released',
					subtype: 'ownership_released',
					artifact_id: 'art-2',
					artifact_type: 'document',
					title: 'beta.md',
					actor_kind: 'agent',
					causal_event_id: 'turn-batch-1',
				},
			},
		]);
		renderList();

		expect(host.textContent).toContain('Released alpha.md, beta.md');
		expect(host.textContent).not.toContain('Removed alpha.md');
		expect(host.querySelectorAll('[data-message-id]')).toHaveLength(1);
	});

	it('does not group two removal events from distinct causal actions', () => {
		messages.set([
			{
				id: 'removal-1',
				thread_id: THREAD_ID,
				role: 'system',
				content: 'Removed alpha.md',
				created_at: '2026-08-15T00:00:00Z',
				metadata: {
					type: 'artifact_removed',
					event_type: 'artifact.hidden_from_pane',
					subtype: 'hidden_from_pane',
					artifact_id: 'art-1',
					artifact_type: 'document',
					title: 'alpha.md',
					actor_kind: 'user',
					causal_event_id: null,
					hidden: true,
				},
			},
			{
				id: 'removal-2',
				thread_id: THREAD_ID,
				role: 'system',
				content: 'Removed beta.md',
				created_at: '2026-08-15T00:00:01Z',
				metadata: {
					type: 'artifact_removed',
					event_type: 'artifact.hidden_from_pane',
					subtype: 'hidden_from_pane',
					artifact_id: 'art-2',
					artifact_type: 'document',
					title: 'beta.md',
					actor_kind: 'user',
					causal_event_id: null,
					hidden: true,
				},
			},
		]);
		renderList();

		expect(host.textContent).toContain('Removed alpha.md');
		expect(host.textContent).toContain('Removed beta.md');
		expect(host.querySelectorAll('[data-message-id]')).toHaveLength(2);
	});

	it('keeps a batched removal row concise without hidden details', () => {
		messages.set([
			{
				id: 'removal-1',
				thread_id: THREAD_ID,
				role: 'system',
				content: 'Removed alpha.md',
				created_at: '2026-08-15T00:00:00Z',
				metadata: {
					type: 'artifact_removed',
					event_type: 'artifact.hidden_from_pane',
					subtype: 'hidden_from_pane',
					artifact_id: 'art-1',
					title: 'alpha.md',
					actor_kind: 'agent',
					causal_event_id: 'turn-batch-2',
				},
			},
			{
				id: 'removal-2',
				thread_id: THREAD_ID,
				role: 'system',
				content: 'Removed beta.md',
				created_at: '2026-08-15T00:00:01Z',
				metadata: {
					type: 'artifact_removed',
					event_type: 'artifact.hidden_from_pane',
					subtype: 'hidden_from_pane',
					artifact_id: 'art-2',
					title: 'beta.md',
					actor_kind: 'agent',
					causal_event_id: 'turn-batch-2',
				},
			},
		]);
		renderList();

		expect(host.textContent).toContain('Removed alpha.md, beta.md');
		expect(host.querySelector('.message-content.context-event')?.tagName).toBe('DIV');
		expect(host.textContent).not.toContain('Technical details');
	});
});
