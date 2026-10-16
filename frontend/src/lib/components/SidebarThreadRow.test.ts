import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import SidebarThreadRow from './SidebarThreadRow.svelte';
import { checkpointsByThread, type CheckpointStatus } from '$lib/stores/checkpoints';
import { threadPlanStates } from '$lib/stores/threadPlanStates';
import { _resetTurnStateForTests, applyLifecycle } from '$lib/ws/turnState';
import type { Thread } from '$lib/stores/threads';
import type { ThreadPlanStateEntry } from '$lib/api/plans';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const thread: Thread = {
	id: 'thread-ctx', user_id: 'steve', profile: 'default',
	title: 'Checkpoint bug investigation', model: null, project_id: 'corsia',
	created_at: '2026-07-22T08:00:00', updated_at: '2026-07-22T08:00:00',
};

function setCheckpoint(
	status: CheckpointStatus,
	trigger: 'manual' | 'pressure' | 'model_switch' = 'manual',
	threadId = thread.id,
): void {
	checkpointsByThread.set(new Map([[threadId, {
		checkpoints: [{
			id: 'checkpoint-1', trigger, mode: 'warm', status,
			writerModel: 'model-a', error: status === 'failed' ? 'failed' : null,
			startedAt: '2026-07-22T08:00:00Z',
		}],
		loading: false,
		error: null,
	}]]));
}

function render(renaming = false): void {
	component = mount(SidebarThreadRow, {
		target: host,
		props: {
			thread,
			isPinned: false,
			isActive: false,
			renaming,
			renameValue: '',
			swiping: false,
			isSwipeGesture: false,
			transformX: 0,
			leftZoneX: -72,
			rightZoneX: 72,
			pinArmed: false,
			deleteArmed: false,
			onTouchStart: () => {},
			onTouchEnd: () => {},
			onSelect: () => {},
			onOpenContextMenu: () => {},
			onPin: () => {},
			onArchive: () => {},
			onDelete: () => {},
			onStartRename: () => {},
			onFinishRename: () => {},
			onCancelRename: () => {},
		},
	});
	flushSync();
}

beforeEach(() => {
	checkpointsByThread.set(new Map());
	threadPlanStates.set({});
	_resetTurnStateForTests();
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	checkpointsByThread.set(new Map());
	threadPlanStates.set({});
	_resetTurnStateForTests();
});

describe('SidebarThreadRow update summary', () => {
	it('renders accessible summary beside a higher failed mark and hides it during rename', () => {
		const summary = 'A deliberately long latest update that remains available in its title';
		threadPlanStates.set({ 'thread-ctx': { state: 'failed', label_key: null, paused: false, attention_items: [{ kind: 'failed', title: 'Fail', node_id: 'n' }], latest_update: { kind: 'failed', summary } } });
		render();
		const line = host.querySelector<HTMLElement>('.thread-update-summary');
		expect(line?.textContent).toBe(summary);
		expect(line?.title).toBe(summary);
		expect(host.querySelector('.plan-state.failed')).not.toBeNull();
		unmount(component!); component = null; host.replaceChildren();
		render(true);
		expect(host.querySelector('.thread-update-summary')).toBeNull();
	});

	it('keeps a row single-line when no update summary exists', () => {
		render();
		expect(host.querySelector('.thread-update-summary')).toBeNull();
	});
});

describe('SidebarThreadRow checkpoint indicator', () => {
	it.each([
		['running', 'manual', 'checkpointing', 'Checkpointing context'],
		['captured', 'pressure', 'checkpointing', 'Checkpointing context'],
		['running', 'model_switch', 'switching', 'Switching model'],
	] as const)('shows %s %s work', (status, trigger, label, title) => {
		setCheckpoint(status, trigger);
		render();

		const indicator = host.querySelector<HTMLElement>('.thread-cutover');
		expect(indicator?.textContent).toBe(label);
		expect(indicator?.title).toBe(title);
	});

	it.each(['installed', 'overtaken', 'failed', 'cancelled'] as const)(
		'clears the indicator on %s',
		(status) => {
			setCheckpoint(status);
			render();
			expect(host.querySelector('.thread-cutover')).toBeNull();
		},
	);

	it('shows only state belonging to this thread', () => {
		setCheckpoint('running', 'manual', 'another-thread');
		render();

		expect(host.querySelector('.thread-cutover')).toBeNull();
		expect(host.textContent).toContain('Checkpoint bug investigation');
	});

	it('reacts when background work starts after the row renders', () => {
		render();
		expect(host.querySelector('.thread-cutover')).toBeNull();

		setCheckpoint('running');
		flushSync();

		expect(host.querySelector('.thread-cutover')?.textContent).toBe('checkpointing');
		expect(host.querySelector('.thread-attention')).toBeNull();
	});
});


describe('SidebarThreadRow motion truth', () => {
	function renderWithActivity(activity?: string): void {
		component = mount(SidebarThreadRow, {
			target: host,
			props: {
				thread,
				isPinned: false,
				isActive: false,
				activity,
				renaming: false,
				renameValue: '',
				swiping: false,
				isSwipeGesture: false,
				transformX: 0,
				leftZoneX: -72,
				rightZoneX: 72,
				pinArmed: false,
				deleteArmed: false,
				onTouchStart: () => {},
				onTouchEnd: () => {},
				onSelect: () => {},
				onOpenContextMenu: () => {},
				onPin: () => {},
				onArchive: () => {},
				onDelete: () => {},
				onStartRename: () => {},
				onFinishRename: () => {},
				onCancelRename: () => {},
			},
		});
		flushSync();
	}

	it('shows the blue spinner while its own Turn runs', () => {
		renderWithActivity('active');

		// The spinner occupies the trailing column so it stays vertically centred
		// whether the row has one line or an update summary.
		const spinner = host.querySelector<SVGElement>('.thread-item > .spinner');
		expect(spinner?.getAttribute('stroke')).toBe('var(--status-in-progress)');
		expect(spinner?.getAttribute('aria-label')).toBe('Working');
	});

	it('shows the gray spinner when only a descendant runs', () => {
		applyLifecycle('subagent-of-thread-ctx', { turnId: 'turn-sub', turnOrder: 3 }, 'thread-ctx');
		renderWithActivity();

		const spinner = host.querySelector<SVGElement>('.thread-item > .spinner');
		expect(spinner?.getAttribute('stroke')).toBe('var(--text-tertiary)');
		expect(spinner?.getAttribute('aria-label')).toBe('Subagent working');
		expect(host.querySelector('.update-dot')).toBeNull();
	});

	it('shows no arc while nothing runs, even with a moving plan', () => {
		threadPlanStates.set({ 'thread-ctx': { state: 'moving', label_key: 'working', paused: false, attention_items: [] } });
		renderWithActivity();

		expect(host.querySelector('.thread-title-row svg')).toBeNull();
	});

	it('leads the title with the pause mark when the plan is paused', () => {
		threadPlanStates.set({ 'thread-ctx': { state: 'paused', label_key: 'waiting', paused: true, attention_items: [] } });
		renderWithActivity();

		const row = host.querySelector<HTMLElement>('.thread-title-row');
		const pause = row?.querySelector<SVGElement>('svg.paused-mark');
		expect(pause?.getAttribute('aria-label')).toBe('Paused');
		// It sits left of the name, and the old trailing glyph is gone.
		expect(row?.firstElementChild?.classList.contains('paused-mark')).toBe(true);
		expect(host.querySelector('.paused-glyph')).toBeNull();
	});
});

describe('SidebarThreadRow Done check (AS16)', () => {
	const settled: ThreadPlanStateEntry = { state: 'idle', label_key: null, paused: false, attention_items: [], settled: true };

	function renderRow(activity?: string): void {
		component = mount(SidebarThreadRow, {
			target: host,
			props: {
				thread, isPinned: false, isActive: false, activity, renaming: false, renameValue: '',
				swiping: false, isSwipeGesture: false, transformX: 0, leftZoneX: -72, rightZoneX: 72,
				pinArmed: false, deleteArmed: false, onTouchStart: () => {}, onTouchEnd: () => {},
				onSelect: () => {}, onOpenContextMenu: () => {}, onPin: () => {}, onArchive: () => {},
				onDelete: () => {}, onStartRename: () => {}, onFinishRename: () => {}, onCancelRename: () => {},
			},
		});
		flushSync();
	}

	it('shows a check left of the name when the plan settled and nothing runs', () => {
		threadPlanStates.set({ [thread.id]: settled });
		renderRow();

		const row = host.querySelector('.thread-title-row');
		expect(row?.firstElementChild?.classList.contains('done-mark')).toBe(true);
		expect(row?.firstElementChild?.getAttribute('aria-label')).toBe('Done');
	});

	it('clears while its own Turn or a descendant runs, and without a settled plan', () => {
		threadPlanStates.set({ [thread.id]: settled });
		applyLifecycle(thread.id, { turnId: 'turn-own', turnOrder: 2 }, thread.id);
		renderRow('active');
		expect(host.querySelector('.done-mark')).toBeNull();
		unmount(component!);
		_resetTurnStateForTests();

		applyLifecycle('subagent-of-thread-ctx', { turnId: 'turn-sub', turnOrder: 3 }, 'thread-ctx');
		renderRow();
		expect(host.querySelector('.done-mark')).toBeNull();
		unmount(component!);
		_resetTurnStateForTests();

		threadPlanStates.set({ [thread.id]: { ...settled, settled: false } });
		renderRow();
		expect(host.querySelector('.done-mark')).toBeNull();
		unmount(component!);

		threadPlanStates.set({ [thread.id]: { ...settled, paused: true } });
		renderRow();
		expect(host.querySelector('.done-mark')).toBeNull();
		expect(host.querySelector('.paused-mark')).not.toBeNull();
	});
});
