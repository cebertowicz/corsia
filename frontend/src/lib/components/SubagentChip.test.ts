import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import {
	_resetSubagentHydrationForTests,
	openSubagentRequest,
	registerSubagent,
	subagentMeta,
} from '$lib/stores/subagents';
import SubagentChip from './SubagentChip.svelte';

const MARKER_MSG = {
	id: 'm1',
	thread_id: 'parent-1',
	role: 'system',
	content: 'Spawned subagent: tmp inspector',
	metadata: {
		type: 'subagent_ref',
		child_thread_id: 'child-1',
		name: 'tmp inspector',
		provider: 'cc',
	},
};

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	subagentMeta.set(new Map());
	openSubagentRequest.set(null);
	_resetSubagentHydrationForTests();
	token.set(null);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
	token.set(null);
	_resetSubagentHydrationForTests();
});

function renderChip(msg: unknown): HTMLElement {
	component = mount(SubagentChip, { target: host, props: { msg } });
	flushSync();
	const chip = host.querySelector<HTMLElement>('button.sa-chip');
	expect(chip).not.toBeNull();
	return chip!;
}

describe('SubagentChip', () => {
	it('renders a chip with NO live meta as unknown — never a word it cannot support', () => {
		const chip = renderChip(MARKER_MSG);
		expect(chip.querySelector('.sa-action')?.textContent).toBe('Spawned worker');
		expect(chip.querySelector('.sa-name')?.textContent).toBe('tmp inspector');
		expect(chip.querySelector('.sa-status')?.textContent).toBe('unknown');
		expect(host.textContent).not.toContain('Spawned subagent:');
		expect(host.textContent).not.toContain('done');
		expect(host.textContent).not.toContain('open');
	});

	it('asks the server what the worker actually did, once, on mount', async () => {
		const fetchMock = vi.fn(async () => ({
			ok: true,
			status: 200,
			json: async () => ({
				thread_id: 'child-1', title: 'tmp inspector', thread_kind: 'subagent',
				driver: 'cc', cc_live: false, codex_live: false, busy: true, turn_active: true,
			}),
		}));
		vi.stubGlobal('fetch', fetchMock as never);
		token.set('test-token');

		const chip = renderChip(MARKER_MSG);
		await vi.waitFor(() => {
			flushSync();
			expect(chip.querySelector('.sa-status')?.textContent).toBe('running');
		});
		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(String(fetchMock.mock.calls[0][0])).toContain('/api/delegation/threads/child-1/status');
	});

	it('stays unknown when the server cannot be reached', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline'); }) as never);
		token.set('test-token');

		const chip = renderChip(MARKER_MSG);
		await vi.waitFor(() => expect(console.error).toHaveBeenCalled());
		flushSync();
		expect(chip.querySelector('.sa-status')?.textContent).toBe('unknown');
	});

	it('live meta overrides the fallback', () => {
		registerSubagent('parent-1', 'child-1', 'tmp inspector', 'cc', 'running');
		const chip = renderChip(MARKER_MSG);
		expect(chip.querySelector('.sa-status')?.textContent).toBe('running');

		registerSubagent('parent-1', 'child-1', 'tmp inspector', 'cc', 'ready');
		flushSync();
		expect(chip.querySelector('.sa-status')?.textContent).toBe('ready');
	});

	it('only shows terminal done after an explicit close status', () => {
		registerSubagent('parent-1', 'child-1', 'tmp inspector', 'cc', 'closed');
		const chip = renderChip(MARKER_MSG);
		expect(chip.querySelector('.sa-status')?.textContent).toBe('done');
	});

	it('survives a marker with missing name and child id (degenerate metadata)', () => {
		const chip = renderChip({
			id: 'm2',
			role: 'system',
			content: 'Spawned subagent: ???',
			metadata: { type: 'subagent_ref' },
		});
		expect(chip.querySelector('.sa-name')?.textContent).toBe('worker');
		chip.click();
		flushSync();
		expect(get(openSubagentRequest)).toBeNull();
	});

	it('click with a child id requests opening the child thread', () => {
		const chip = renderChip(MARKER_MSG);
		chip.click();
		flushSync();
		expect(get(openSubagentRequest)).toBe('child-1');
	});

	it('click restores parent linkage from the persisted marker before opening', () => {
		const chip = renderChip(MARKER_MSG);
		chip.click();
		flushSync();

		expect(get(openSubagentRequest)).toBe('child-1');
		expect(get(subagentMeta).get('child-1')).toMatchObject({
			childThreadId: 'child-1',
			parentThreadId: 'parent-1',
			name: 'tmp inspector',
			// Opening a thread restores linkage. It learns nothing about the
			// worker, so it may not assert anything about the worker.
			status: 'unknown',
		});
	});

	it('renders delegation provider marker with thread kind and model fields', () => {
		const chip = renderChip({
			id: 'm3',
			role: 'system',
			content: 'Spawned: PR Reviewer',
			metadata: {
				type: 'subagent_ref',
				child_thread_id: 'deleg-child-1',
				name: 'PR Reviewer',
				provider: 'delegation',
				thread_kind: 'subagent',
				model: 'anthropic/claude-3-5-sonnet',
			},
		});
		expect(chip.querySelector('.sa-name')?.textContent).toBe('PR Reviewer');
		expect(chip.querySelector('.sa-action')?.textContent).toBe('Spawned worker');
		expect(host.textContent).not.toContain('delegation');
		expect(host.textContent).not.toContain('anthropic/claude-3-5-sonnet');
	});

	it('shows ⌘ glyph for CC provider', () => {
		const chip = renderChip({ ...MARKER_MSG, metadata: { ...MARKER_MSG.metadata, provider: 'cc' } });
		expect(chip.querySelector('.sa-glyph')?.textContent).toBe('⌘');
	});

	it('shows ⊙ glyph for codex provider', () => {
		const chip = renderChip({
			id: 'm5', role: 'system', content: 'Spawned: Codex Worker',
			metadata: { type: 'subagent_ref', child_thread_id: 'codex-child', name: 'Codex Worker', provider: 'codex' },
		});
		expect(chip.querySelector('.sa-glyph')?.textContent).toBe('⊙');
	});

	it('shows ◈ glyph for native/oMLX provider', () => {
		const chip = renderChip({
			id: 'm6', role: 'system', content: 'Spawned: Native Worker',
			metadata: { type: 'subagent_ref', child_thread_id: 'native-child', name: 'Native Worker', provider: 'native' },
		});
		expect(chip.querySelector('.sa-glyph')?.textContent).toBe('◈');
	});

	it('renders visible thread markers as links, not completed subagents', () => {
		const chip = renderChip({
			id: 'm4',
			thread_id: 'parent-1',
			role: 'system',
			content: 'Spawned: Peer',
			metadata: {
				type: 'subagent_ref',
				child_thread_id: 'deleg-child-2',
				name: 'Peer',
				provider: 'delegation',
				thread_kind: 'chat',
				model: 'anthropic/claude-3-opus',
			},
		});
		expect(chip.querySelector('.sa-action')?.textContent).toBe('Spawned thread');
		expect(chip.querySelector('.sa-status')?.textContent).toBe('open');
		expect(host.textContent).not.toContain('done');

		chip.click();
		flushSync();
		expect(get(openSubagentRequest)).toBe('deleg-child-2');
		// A peer thread is NOT a worker. Putting it in the subagent map would
		// route its sends to a child stdin channel that does not exist.
		expect(get(subagentMeta).has('deleg-child-2')).toBe(false);
	});
});
