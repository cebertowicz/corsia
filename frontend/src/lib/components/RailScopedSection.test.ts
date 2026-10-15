import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { activeThreadId, threads } from '$lib/stores/threads';
import { canvasHeader } from '$lib/stores/canvasHeader';
import { uiScope } from '$lib/stores/uiScope';
import { threadPlanStates } from '$lib/stores/threadPlanStates';
import * as productEvents from '$lib/product_events';
import RailScopedSection from './RailScopedSection.svelte';
import {
	_resetTurnStateForTests,
	applyLifecycle,
	applyTerminal,
} from '$lib/ws/turnState';

const CONDUCTOR_TURN = { turnId: 'turn-conductor', turnOrder: 1 };

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	canvasHeader.set(null);
	uiScope.set(null);
	threads.set([
		{
			id: 'chat-1',
			user_id: 'u-test',
			profile: 'personal',
			title: 'Visible Chat',
			model: 'mock/model',
			created_at: '2026-06-18T00:00:00',
			updated_at: '2026-06-18T00:00:00',
		},
		{
			id: 'agent-1',
			user_id: 'u-test',
			profile: 'personal',
			title: 'Bug-Fixer',
			model: 'mock/model',
			agent_enabled: 1,
			agent_status: 'active',
			agent_responsibility: 'Owns bounded, reproducible bugs',
			created_at: '2026-06-18T00:00:00',
			updated_at: '2026-06-18T00:00:00',
		},
	]);
	activeThreadId.set('chat-1');
	threadPlanStates.set({});
	_resetTurnStateForTests();
});

afterEach(() => {
	vi.useRealTimers();
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	uiScope.set(null);
	threads.set([]);
	threadPlanStates.set({});
	_resetTurnStateForTests();
});

function makeAgent(id: string, title: string) {
	return {
		id,
		user_id: 'u-test',
		profile: 'personal',
		title,
		model: 'mock/model',
		agent_enabled: 1,
		agent_status: 'active',
		agent_responsibility: `Owns ${title}`,
		created_at: '2026-06-18T00:00:00',
		updated_at: '2026-06-18T00:00:00',
	};
}

function renderRail(
	onOpenContextMenu?: (event: MouseEvent, id: string, kind: 'agent' | 'conductor') => void,
	onSelectThread: (id: string) => void = () => {},
	overrides: Record<string, unknown> = {},
) {
	component = mount(RailScopedSection, {
		target: host,
		props: {
			conductorThreadId: 'conductor-1',
			onConductor: () => {},
			onSelectThread,
			onOpenContextMenu,
			...overrides,
		},
	});
	flushSync();
}

describe('RailScopedSection agent zone', () => {
	it('renders the conductor and persistent agents; not Documents or Automations', () => {
		renderRail();

		const labels = Array.from(host.querySelectorAll('button')).map((button) =>
			button.textContent?.trim(),
		);
		expect(labels).toContain('CORSIA');
		expect(labels).not.toContain('Conductor');
		// Documents + Automations moved off this surface.
		expect(labels).not.toContain('Documents');
		expect(labels).not.toContain('Automations');

		// The persistent Agent appears here (once), carrying the agent row class.
		const agentRow = host.querySelector('.scoped-item.agent');
		expect(agentRow).not.toBeNull();
		expect(agentRow?.textContent).toContain('Bug-Fixer');
	});

	it('renders an accessible Agent summary beside a higher mark and hides it while renaming', () => {
		const summary = 'A deliberately long latest update that remains available in its title';
		threadPlanStates.set({
			'agent-1': { state: 'failed', label_key: null, paused: false, attention_items: [{ kind: 'failed', title: 'Failure', node_id: 'node-1' }], latest_update: { kind: 'failed', summary } },
		});
		renderRail();
		const row = host.querySelector<HTMLElement>('.scoped-item.agent')!;
		expect(row.querySelector<HTMLElement>('.update-summary')?.title).toBe(summary);
		expect(row.querySelector('.plan-state.failed')).not.toBeNull();
		expect(row.querySelector('[role="button"]')?.getAttribute('aria-label')).toContain(summary);
		(component as { startRename: (id: string) => void }).startRename('agent-1');
		flushSync();
		expect(row.querySelector('.update-summary')).toBeNull();
	});

	it('renders the Conductor latest-update summary beside its mark and omits it when missing', () => {
		const summary = 'A deliberately long Conductor update that remains available in its full title';
		threadPlanStates.set({
			'conductor-1': { state: 'failed', label_key: null, paused: false, attention_items: [{ kind: 'failed', title: 'Failure', node_id: 'node-1' }], latest_update: { kind: 'failed', summary } },
		});
		renderRail();
		const row = host.querySelector<HTMLElement>('.scoped-item.conductor')!;
		expect(row.querySelector<HTMLElement>('.update-summary')?.title).toBe(summary);
		expect(row.querySelector('.plan-state.failed')).not.toBeNull();
		expect(row.querySelector<HTMLButtonElement>('.scoped-item-trigger')?.getAttribute('aria-label')).toContain(summary);

		threadPlanStates.set({
			'conductor-1': { state: 'idle', label_key: null, paused: false, attention_items: [] },
		});
		flushSync();
		expect(row.querySelector('.update-summary')).toBeNull();
	});

	it('keeps the plan-state mark a sibling of the row control, never nested inside it', () => {
		threadPlanStates.set({
			'conductor-1': {
				state: 'needs_input', label_key: null, paused: false,
				attention_items: [{ kind: 'decision', title: 'Choose a model', node_id: 'node-1' }],
			},
			'agent-1': {
				state: 'needs_input', label_key: null, paused: false,
				attention_items: [{ kind: 'decision', title: 'Choose a model', node_id: 'node-2' }],
			},
		});
		renderRail();

		for (const rowSelector of ['.scoped-item.conductor', '.scoped-item.agent']) {
			const row = host.querySelector<HTMLElement>(rowSelector)!;
			const rowControl = row.matches('button, [role="button"]')
				? row
				: row.querySelector('button, [role="button"]')!;
			expect(rowControl.querySelector('.plan-state')).toBeNull();
			expect(row.querySelector('.plan-state')).not.toBeNull();
		}
	});

	it('labels a project scope\'s entry Conductor, not CORSIA', () => {
		uiScope.set({ id: 'project-1', name: 'Project One', color: '#8b5cf6' });
		renderRail();

		const labels = Array.from(host.querySelectorAll('button')).map((button) =>
			button.textContent?.trim(),
		);
		expect(labels).toContain('Conductor');
		expect(labels).not.toContain('CORSIA');
	});

	it('renders the Conductor entry disabled when the scope has no designation', () => {
		const onConductor = vi.fn();
		renderRail(undefined, undefined, {
			conductorThreadId: null,
			conductorKnownAbsent: true,
			onConductor,
		});

		const conductor = host.querySelector<HTMLElement>('.scoped-item.conductor');
		expect(conductor).not.toBeNull();
		expect(conductor?.querySelector<HTMLButtonElement>('.scoped-item-trigger')?.disabled).toBe(true);
		expect(conductor?.getAttribute('title')).toBe('No CORSIA agent for this scope');
		conductor?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
		expect(onConductor).not.toHaveBeenCalled();
	});

	it('keeps the Conductor entry clickable while its designation is unresolved', () => {
		const onConductor = vi.fn();
		renderRail(undefined, undefined, {
			conductorThreadId: null,
			conductorKnownAbsent: false,
			onConductor,
		});

		const conductor = host.querySelector<HTMLElement>('.scoped-item.conductor');
		expect(conductor).not.toBeNull();
		expect(conductor?.querySelector<HTMLButtonElement>('.scoped-item-trigger')?.disabled).toBe(false);
		expect(conductor?.getAttribute('title')).toBe('Open CORSIA chat');
		conductor?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
		expect(onConductor).toHaveBeenCalledOnce();
	});

	it('keeps workspace and project Agent rosters isolated', () => {
		threads.set([
			makeAgent('agent-workspace', 'Workspace Owner'),
			{ ...makeAgent('agent-project', 'Project Owner'), project_id: 'project-1' },
		]);
		renderRail();

		expect(host.textContent).toContain('Workspace Owner');
		expect(host.textContent).not.toContain('Project Owner');

		unmount(component!);
		component = null;
		host.replaceChildren();
		uiScope.set({ id: 'project-1', name: 'Project One', color: '#8b5cf6' });
		renderRail();

		expect(host.textContent).toContain('Project Owner');
		expect(host.textContent).not.toContain('Workspace Owner');
	});

	it('does not render ordinary conversations in the agent zone', () => {
		renderRail();
		// The non-agent chat lives in the conversation list, never the zone.
		const text = host.textContent ?? '';
		expect(text).not.toContain('Visible Chat');
	});

	it('shows the conductor spinner when its hidden session is active', () => {
		applyLifecycle('conductor-1', CONDUCTOR_TURN);
		renderRail();

		expect(host.querySelector('.scoped-item.conductor .spinner')).not.toBeNull();
		expect(host.querySelector('.scoped-item.conductor .update-dot')).toBeNull();
	});

	it('shows the conductor update dot when the server reports an update since the last visit', () => {
		applyLifecycle('conductor-1', CONDUCTOR_TURN);
		applyTerminal('conductor-1', CONDUCTOR_TURN);
		threadPlanStates.set({ 'conductor-1': { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true } });
		renderRail();

		expect(host.querySelector('.scoped-item.conductor .update-dot')).not.toBeNull();
		expect(host.querySelector('.scoped-item.conductor .spinner')).toBeNull();
	});

	it('waits for asynchronous conductor navigation before reporting the dot open', async () => {
		const spy = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		let finishOpen: (() => void) | undefined;
		const onConductor = vi.fn(() => new Promise<void>((resolve) => { finishOpen = resolve; }));
		threadPlanStates.set({ 'conductor-1': { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true } });
		renderRail(undefined, undefined, { onConductor });

		host.querySelector<HTMLElement>('.scoped-item.conductor .update-dot')?.click();
		expect(spy).not.toHaveBeenCalled();
		finishOpen?.();

		expect(onConductor).toHaveBeenCalledOnce();
		await vi.waitFor(() => {
			expect(spy.mock.calls).toEqual([['sidebar', 'update_open', { outcome: 'succeeded' }]]);
		});
		spy.mockRestore();
	});

	it('reports a declined conductor navigation without claiming success', async () => {
		const spy = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		const onConductor = vi.fn(async () => false);
		threadPlanStates.set({ 'conductor-1': { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true } });
		renderRail(undefined, undefined, { onConductor });

		host.querySelector<HTMLElement>('.scoped-item.conductor .update-dot')?.click();

		await vi.waitFor(() => {
			expect(spy.mock.calls).toEqual([['sidebar', 'update_open', { outcome: 'refused', reason: 'not_opened' }]]);
		});
		expect(onConductor).toHaveBeenCalledOnce();
		spy.mockRestore();
	});

	it('reports a rejected conductor navigation as failed', async () => {
		const spy = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		const report = vi.fn();
		vi.stubGlobal('reportError', report);
		const failure = new Error('private conductor failure');
		const onConductor = vi.fn(async () => { throw failure; });
		threadPlanStates.set({ 'conductor-1': { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true } });
		renderRail(undefined, undefined, { onConductor });

		host.querySelector<HTMLElement>('.scoped-item.conductor .update-dot')?.click();

		await vi.waitFor(() => {
			expect(spy.mock.calls).toEqual([['sidebar', 'update_open', { outcome: 'failed', reason: 'open_failed' }]]);
		});
		expect(report).toHaveBeenCalledWith(failure);
		expect(JSON.stringify(spy.mock.calls)).not.toContain('private');
		vi.unstubAllGlobals();
		spy.mockRestore();
	});

	it('does not show the conductor update dot while the conductor thread is open', () => {
		applyLifecycle('conductor-1', CONDUCTOR_TURN);
		applyTerminal('conductor-1', CONDUCTOR_TURN);
		threadPlanStates.set({ 'conductor-1': { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true } });
		activeThreadId.set('conductor-1');
		renderRail();

		expect(host.querySelector('.scoped-item.conductor .update-dot')).toBeNull();
	});

	it('caps the collapsed drawer at five and exposes an accented unread summary', () => {
		threads.set([
			makeAgent('agent-a', 'Alpha'),
			makeAgent('agent-b', 'Bravo'),
			makeAgent('agent-c', 'Charlie'),
			makeAgent('agent-d', 'Delta'),
			makeAgent('agent-e', 'Echo'),
			makeAgent('agent-f', 'Foxtrot'),
			makeAgent('agent-g', 'Golf'),
		]);
		for (const id of ['agent-b', 'agent-c', 'agent-d', 'agent-e', 'agent-f', 'agent-g']) {
			const identity = { turnId: `turn-${id}`, turnOrder: 1 };
			applyLifecycle(id, identity);
			applyTerminal(id, identity);
		}
		threadPlanStates.set(Object.fromEntries(
			['agent-b', 'agent-c', 'agent-d', 'agent-e', 'agent-f', 'agent-g'].map((id) => [id, { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true }]),
		));
		renderRail();

		expect(host.querySelectorAll('.scoped-item.agent')).toHaveLength(5);
		const toggle = host.querySelector<HTMLButtonElement>('.drawer-toggle');
		expect(toggle?.textContent?.replace(/\s+/g, ' ').trim()).toBe('Show 2 more · 1 unread');
		expect(toggle?.querySelector('.drawer-attention')).not.toBeNull();

		toggle?.click();
		flushSync();
		expect(host.querySelectorAll('.scoped-item.agent')).toHaveLength(7);
		expect(toggle?.textContent?.replace(/\s+/g, ' ').trim()).toBe('Show less');
	});

	it('shows hidden working attention in the overflow summary', () => {
		threads.set([
			makeAgent('agent-a', 'Alpha'),
			makeAgent('agent-b', 'Bravo'),
			makeAgent('agent-c', 'Charlie'),
			makeAgent('agent-d', 'Delta'),
			makeAgent('agent-e', 'Echo'),
			makeAgent('agent-f', 'Foxtrot'),
			makeAgent('agent-g', 'Golf'),
		]);
		for (const id of ['agent-b', 'agent-c', 'agent-d', 'agent-e', 'agent-f', 'agent-g']) {
			applyLifecycle(id, { turnId: `turn-${id}`, turnOrder: 1 });
		}
		renderRail();

		const toggle = host.querySelector<HTMLButtonElement>('.drawer-toggle');
		expect(toggle?.textContent?.replace(/\s+/g, ' ').trim()).toBe('Show 2 more · 2 working');
		expect(toggle?.querySelector('.drawer-attention')).not.toBeNull();
	});

	it('opens attention through the sidebar callback without opening the conductor row', () => {
		const onConductor = vi.fn();
		const onOpenAttention = vi.fn();
		threadPlanStates.set({
			'conductor-1': {
				state: 'needs_input', label_key: null, paused: false,
				attention_items: [{ kind: 'decision', title: 'Choose a model', node_id: 'node-1' }],
			},
		});
		renderRail(undefined, undefined, { onConductor, onOpenAttention });

		host.querySelector<HTMLElement>('.scoped-item.conductor .plan-state')?.click();
		expect(onOpenAttention).toHaveBeenCalledWith('conductor-1', 'Decide');
		expect(onConductor).not.toHaveBeenCalled();
	});

	it('opens the Agent and Conductor context menu on right click', () => {
		const onOpenContextMenu = vi.fn();
		renderRail(onOpenContextMenu);

		host.querySelector('.scoped-item.agent')?.dispatchEvent(new MouseEvent('contextmenu', {
			bubbles: true,
			clientX: 40,
			clientY: 60,
		}));
		host.querySelector('.scoped-item.conductor')?.dispatchEvent(new MouseEvent('contextmenu', {
			bubbles: true,
			clientX: 20,
			clientY: 30,
		}));

		expect(onOpenContextMenu).toHaveBeenNthCalledWith(1, expect.any(MouseEvent), 'agent-1', 'agent');
		expect(onOpenContextMenu).toHaveBeenNthCalledWith(2, expect.any(MouseEvent), 'conductor-1', 'conductor');
	});

	it('opens an Agent context menu after touch hold and suppresses the following click', () => {
		vi.useFakeTimers();
		const onOpenContextMenu = vi.fn();
		const onSelectThread = vi.fn();
		renderRail(onOpenContextMenu, onSelectThread);

		const agentRow = host.querySelector<HTMLElement>('.scoped-item.agent');
		expect(agentRow).not.toBeNull();
		const pointerDown = new MouseEvent('pointerdown', {
			bubbles: true,
			clientX: 40,
			clientY: 60,
		});
		Object.defineProperty(pointerDown, 'pointerType', { value: 'touch' });
		agentRow?.dispatchEvent(pointerDown);
		vi.advanceTimersByTime(500);

		expect(onOpenContextMenu).toHaveBeenCalledWith(expect.any(MouseEvent), 'agent-1', 'agent');
		agentRow?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
		expect(onSelectThread).not.toHaveBeenCalled();
	});

	it('does not let a long hold swallow a later independent tap', () => {
		vi.useFakeTimers();
		const onSelectThread = vi.fn();
		renderRail(vi.fn(), onSelectThread);
		const agentRow = host.querySelector<HTMLElement>('.scoped-item.agent');
		expect(agentRow).not.toBeNull();

		const heldPointer = new MouseEvent('pointerdown', { bubbles: true, clientX: 40, clientY: 60 });
		Object.defineProperty(heldPointer, 'pointerType', { value: 'touch' });
		agentRow?.dispatchEvent(heldPointer);
		vi.advanceTimersByTime(500);

		const nextPointer = new MouseEvent('pointerdown', { bubbles: true, clientX: 40, clientY: 60 });
		Object.defineProperty(nextPointer, 'pointerType', { value: 'touch' });
		agentRow?.dispatchEvent(nextPointer);
		agentRow?.dispatchEvent(new MouseEvent('pointerup', { bubbles: true }));
		agentRow?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
		expect(onSelectThread).toHaveBeenCalledOnce();
	});

	it('keeps Agent rename keyboard input inside the input instead of selecting the row', () => {
		const onSelectThread = vi.fn();
		renderRail(undefined, onSelectThread);
		(component as { startRename: (id: string) => void }).startRename('agent-1');
		flushSync();

		const input = host.querySelector<HTMLInputElement>('.rename-input');
		expect(input).not.toBeNull();
		const space = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
		input?.dispatchEvent(space);
		expect(space.defaultPrevented).toBe(false);
		expect(onSelectThread).not.toHaveBeenCalled();

		const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
		input?.dispatchEvent(enter);
		expect(enter.defaultPrevented).toBe(true);
		expect(onSelectThread).not.toHaveBeenCalled();
	});
});

describe('RailScopedSection — workspace landing', () => {
	it('keeps CORSIA as the workspace landing and renders no Plans destination', () => {
		activeThreadId.set(null);
		renderRail(undefined, undefined, { activeView: 'chat' });
		const labels = Array.from(host.querySelectorAll('button')).map((button) =>
			button.textContent?.trim(),
		);
		expect(labels).toContain('CORSIA');
		expect(labels).not.toContain('Plans');
		expect(host.querySelector('.scoped-item.conductor')?.classList.contains('active')).toBe(true);
	});
});


describe('RailScopedSection motion truth', () => {
	const DESCENDANT = { turnId: 'turn-sub', turnOrder: 3 };

	it('shows the blue spinner only while the agent row own Turn runs', () => {
		applyLifecycle('agent-1', { turnId: 'turn-agent', turnOrder: 1 });
		renderRail();

		const spinner = host.querySelector<SVGElement>('.scoped-item.agent .spinner');
		expect(spinner?.getAttribute('stroke')).toBe('var(--accent)');
		expect(spinner?.getAttribute('aria-label')).toBe('Working');
	});

	it('shows the gray spinner when only a descendant runs, and no dot', () => {
		applyLifecycle('subagent-of-agent-1', DESCENDANT, 'agent-1');
		renderRail();

		const spinner = host.querySelector<SVGElement>('.scoped-item.agent .spinner');
		expect(spinner?.getAttribute('stroke')).toBe('var(--text-tertiary)');
		expect(spinner?.getAttribute('aria-label')).toBe('Subagent working');
		expect(host.querySelector('.scoped-item.agent .update-dot')).toBeNull();
	});

	it('shows no arc at all when nothing runs under the thread, even while the plan is moving', () => {
		// The old static gray 'moving' arc misled when a delegate stalled; plan
		// state no longer claims motion.
		threadPlanStates.set({ 'agent-1': { state: 'moving', label_key: 'working', paused: false, attention_items: [] } });
		renderRail();

		expect(host.querySelector('.scoped-item.agent .spinner')).toBeNull();
		expect(host.querySelector('.scoped-item.agent svg.moving-arc')).toBeNull();
	});

	it('prefers the own-turn spinner while both the row and a descendant run', () => {
		applyLifecycle('subagent-of-agent-1', DESCENDANT, 'agent-1');
		applyLifecycle('agent-1', { turnId: 'turn-agent', turnOrder: 2 });
		renderRail();

		const spinner = host.querySelector<SVGElement>('.scoped-item.agent .spinner');
		expect(spinner?.getAttribute('stroke')).toBe('var(--accent)');
	});

	it('replaces the agent sparkle with the leading pause mark when the plan is paused', () => {
		threadPlanStates.set({ 'agent-1': { state: 'paused', label_key: 'waiting', paused: true, attention_items: [] } });
		renderRail();

		const row = host.querySelector<HTMLElement>('.scoped-item.agent');
		const pause = row?.querySelector<SVGElement>('svg.paused-mark');
		expect(pause?.getAttribute('aria-label')).toBe('Paused');
		// The sparkle is gone for a paused row, and no trailing pause glyph.
		expect(row?.querySelector('svg[aria-hidden="true"] path[d^="M12 3l1.8"]')).toBeNull();
		expect(host.querySelector('.scoped-item.agent .paused-glyph')).toBeNull();
	});

	it('keeps the owed words trailing a paused agent row without resurrecting the old glyph', () => {
		threadPlanStates.set({ 'agent-1': { state: 'needs_input', label_key: 'waiting', paused: true, attention_items: [] } });
		renderRail();

		expect(host.querySelector<HTMLElement>('.scoped-item.agent svg.paused-mark')).not.toBeNull();
		expect(host.querySelector<HTMLElement>('.scoped-item.agent .plan-state')?.textContent).toBe('Needs input');
		expect(host.querySelector('.scoped-item.agent .paused-glyph')).toBeNull();
	});

	it('shows the gray spinner on the conductor row when a descendant of it runs', () => {
		applyLifecycle('subagent-of-conductor', DESCENDANT, 'conductor-1');
		renderRail();

		const spinner = host.querySelector<SVGElement>('.scoped-item.conductor .spinner');
		expect(spinner?.getAttribute('stroke')).toBe('var(--text-tertiary)');
		expect(spinner?.getAttribute('aria-label')).toBe('Subagent working');
	});
});
