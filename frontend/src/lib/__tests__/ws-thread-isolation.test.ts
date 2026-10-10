import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import {
	activeThreadId,
	liveReasoning,
	streamContent,
	activeTools,
	liveReasoningByThread,
	streamContentByThread,
	activeToolsByThread,
	appendThreadReasoning,
	appendThreadContent,
	setThreadReasoning,
	setThreadContent,
	setThreadActiveTools,
	updateThreadActiveTools,
	clearThreadBuffers,
	type ActiveTool,
} from '../stores/threads';


beforeEach(() => {
	activeThreadId.set(null);
	liveReasoningByThread.set(new Map());
	streamContentByThread.set(new Map());
	activeToolsByThread.set(new Map());
});


describe('per-session buffer isolation', () => {
	it('writes to session A do not leak into session B derived views', () => {
		appendThreadReasoning('A', 'reasoning-A');
		appendThreadContent('A', 'content-A');
		setThreadActiveTools('A', [{ id: 't1', name: 'read_file', hint: '', status: 'running' }]);

		activeThreadId.set('B');
		expect(get(liveReasoning)).toBe('');
		expect(get(streamContent)).toBe('');
		expect(get(activeTools)).toEqual([]);
	});

	it('derived views expose exactly the active session buffer', () => {
		appendThreadReasoning('A', 'thinking-A');
		appendThreadReasoning('B', 'thinking-B');

		activeThreadId.set('A');
		expect(get(liveReasoning)).toBe('thinking-A');

		activeThreadId.set('B');
		expect(get(liveReasoning)).toBe('thinking-B');
	});

	it('appendThreadReasoning accumulates within a session', () => {
		appendThreadReasoning('A', 'hello ');
		appendThreadReasoning('A', 'world');
		activeThreadId.set('A');
		expect(get(liveReasoning)).toBe('hello world');
	});

	it('appendThreadContent accumulates within a session', () => {
		appendThreadContent('A', 'part1');
		appendThreadContent('A', 'part2');
		activeThreadId.set('A');
		expect(get(streamContent)).toBe('part1part2');
	});

	it('setThreadReasoning replaces without touching other threads', () => {
		appendThreadReasoning('A', 'original-A');
		appendThreadReasoning('B', 'original-B');
		setThreadReasoning('A', 'replaced-A');

		activeThreadId.set('A');
		expect(get(liveReasoning)).toBe('replaced-A');
		activeThreadId.set('B');
		expect(get(liveReasoning)).toBe('original-B');
	});

	it('updateThreadActiveTools only mutates target session tool list', () => {
		setThreadActiveTools('A', [{ id: 't1', name: 'x', hint: '', status: 'running' }]);
		setThreadActiveTools('B', [{ id: 't2', name: 'y', hint: '', status: 'running' }]);

		updateThreadActiveTools('A', tools =>
			tools.map(t => t.id === 't1' ? { ...t, status: 'done' as const } : t)
		);

		activeThreadId.set('A');
		expect(get(activeTools)[0].status).toBe('done');
		activeThreadId.set('B');
		expect(get(activeTools)[0].status).toBe('running');
	});
});


describe('clearThreadBuffers isolation', () => {
	it('clears only the target session', () => {
		appendThreadReasoning('A', 'reasoning-A');
		appendThreadContent('A', 'content-A');
		setThreadActiveTools('A', [{ id: 't1', name: 'x', hint: '', status: 'running' }]);

		appendThreadReasoning('B', 'reasoning-B');
		appendThreadContent('B', 'content-B');
		setThreadActiveTools('B', [{ id: 't2', name: 'y', hint: '', status: 'running' }]);

		clearThreadBuffers('A');

		activeThreadId.set('A');
		expect(get(liveReasoning)).toBe('');
		expect(get(streamContent)).toBe('');
		expect(get(activeTools)).toEqual([]);

		activeThreadId.set('B');
		expect(get(liveReasoning)).toBe('reasoning-B');
		expect(get(streamContent)).toBe('content-B');
		expect(get(activeTools).length).toBe(1);
	});

	it('is idempotent when session has no buffer', () => {
		expect(() => clearThreadBuffers('nonexistent')).not.toThrow();
	});
});


describe('Bug 7 regression — derived stores reject `.set()`', () => {
	it('liveReasoning is read-only (no .set method)', () => {
		expect((liveReasoning as any).set).toBeUndefined();
		expect((streamContent as any).set).toBeUndefined();
		expect((activeTools as any).set).toBeUndefined();
	});

	it('clearThreadBuffers is the canonical reset path for new threads', () => {
		setThreadContent('new-sid', 'stale');
		setThreadReasoning('new-sid', 'stale');
		setThreadActiveTools('new-sid', [{ id: 't', name: 'x', hint: '', status: 'running' }]);
		clearThreadBuffers('new-sid');
		activeThreadId.set('new-sid');
		expect(get(streamContent)).toBe('');
		expect(get(liveReasoning)).toBe('');
		expect(get(activeTools)).toEqual([]);
	});
});


describe('active session switch', () => {
	it('swaps derived views instantaneously with no mutation', () => {
		appendThreadReasoning('A', 'A-thoughts');
		appendThreadReasoning('B', 'B-thoughts');

		activeThreadId.set('A');
		expect(get(liveReasoning)).toBe('A-thoughts');

		activeThreadId.set('B');
		expect(get(liveReasoning)).toBe('B-thoughts');

		activeThreadId.set('A');
		expect(get(liveReasoning)).toBe('A-thoughts');
	});
});
