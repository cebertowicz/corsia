import { get } from 'svelte/store';
import {
	subagentMeta, openSubagentRequest,
	registerSubagent, setSubagentStatus, setSubagentInit,
	resolveSubagentOpenTarget,
} from './subagents';

beforeEach(() => {
	subagentMeta.set(new Map());
	openSubagentRequest.set(null);
});

describe('subagentMeta', () => {
	it('registers a subagent with parent linkage', () => {
		registerSubagent('parent', 'child', 'fixer');
		const ref = get(subagentMeta).get('child');
		expect(ref?.parentThreadId).toBe('parent');
		expect(ref?.name).toBe('fixer');
		expect(ref?.status).toBe('running');
	});

	it('status updates produce a NEW ref object (Svelte 5 reference tracking)', () => {
		registerSubagent('p', 'c', 's');
		const before = get(subagentMeta).get('c');
		setSubagentStatus('c', 'ready');
		const after = get(subagentMeta).get('c');
		expect(after).not.toBe(before);
		expect(after?.status).toBe('ready');
	});

	it('setSubagentStatus on unknown id is a no-op', () => {
		setSubagentStatus('missing', 'closed');
		expect(get(subagentMeta).size).toBe(0);
	});

	it('init info merges and survives re-registration', () => {
		registerSubagent('p', 'c', 's');
		setSubagentInit('c', { cwd: '/x' });
		registerSubagent('p', 'c', 's', 'cc', 'closed');
		const ref = get(subagentMeta).get('c');
		expect(ref?.cwd).toBe('/x');
		expect(ref?.status).toBe('closed');
	});

	it('openSubagentRequest set/clear', () => {
		openSubagentRequest.set('c1');
		expect(get(openSubagentRequest)).toBe('c1');
		openSubagentRequest.set(null);
		expect(get(openSubagentRequest)).toBeNull();
	});
});

describe('resolveSubagentOpenTarget', () => {
	it('resolves the parent and title from subagentMeta', () => {
		registerSubagent('parent-1', 'child-1', 'Fix: auth bug');
		const target = resolveSubagentOpenTarget('child-1', get(subagentMeta));
		expect(target).toEqual({ parentThreadId: 'parent-1', childThreadId: 'child-1', title: 'Fix: auth bug' });
	});

	it('is null when the child is not in subagentMeta', () => {
		expect(resolveSubagentOpenTarget('unknown', get(subagentMeta))).toBeNull();
	});

	it('is null when the parent link is unknown, so the chip stays inert rather than opening a broken way back', () => {
		const meta = new Map([['child-2', {
			childThreadId: 'child-2', parentThreadId: '', name: 'Worker', provider: 'cc', status: 'unknown' as const,
		}]]);
		expect(resolveSubagentOpenTarget('child-2', meta)).toBeNull();
	});
});
