import { describe, expect, it } from 'vitest';
import type { Thread } from '$lib/stores/threads';
import { createSidebarSortMemo, selectAgentRows, sortThreadsBySidebarPriority, type SidebarStates } from './agentVisibility';

function agent(id: string, title: string): Thread {
	return {
		id,
		user_id: 'user-test',
		profile: 'personal',
		title,
		model: 'mock/model',
		agent_enabled: 1,
		agent_status: 'active',
		agent_responsibility: `Owns ${title}`,
		created_at: '2026-07-29T00:00:00Z',
		updated_at: '2026-07-29T00:00:00Z',
	};
}

const AGENTS = [
	agent('a', 'Alpha'),
	agent('b', 'Bravo'),
	agent('c', 'Charlie'),
	agent('d', 'Delta'),
	agent('e', 'Echo'),
	agent('f', 'Foxtrot'),
	agent('g', 'Golf'),
];

describe('sidebar ordering', () => {
	it('orders owed, failed, update, then quiet without letting working motion win and retains ties', () => {
		const states = {
			b: { state: 'failed', label_key: null, paused: false, attention_items: [] },
			c: { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true },
			d: { state: 'needs_input', label_key: null, paused: false, attention_items: [] },
		};
		expect(sortThreadsBySidebarPriority(AGENTS.slice(0, 5), states).map((row) => row.id)).toEqual(['d', 'b', 'c', 'a', 'e']);
	});

	it('reuses the ordered rows when the same thread objects are passed again', () => {
		const memo = createSidebarSortMemo();
		const threads = AGENTS.slice(0, 2);
		const first = memo(threads, {});
		expect(memo([...threads], {})).toBe(first);
	});

	it('keeps the cached order but returns current thread objects when a field changes', () => {
		const memo = createSidebarSortMemo();
		memo(AGENTS.slice(0, 2), {});
		const renamed = { ...AGENTS[0], title: 'Renamed' };
		const next = memo([renamed, AGENTS[1]], {});
		expect(next[0]).toBe(renamed);
		expect(next.map((row) => row.id)).toEqual([AGENTS[0].id, AGENTS[1].id]);
	});

	it('re-sorts when plan priority changes', () => {
		const memo = createSidebarSortMemo();
		const threads = AGENTS.slice(0, 2);
		memo(threads, {});
		const states = { [threads[1].id]: { state: 'failed', label_key: null, paused: false, attention_items: [] } } as unknown as SidebarStates;
		expect(memo(threads, states).map((row) => row.id)).toEqual([threads[1].id, threads[0].id]);
	});
});

describe('selectAgentRows', () => {
	it('caps the collapsed drawer at five and lets attention replace idle rows', () => {
		const result = selectAgentRows(
			AGENTS,
			{ f: 'done', g: 'active' },
			{ f: { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true } },
			null,
			false,
		);

		expect(result.visible.map((row) => row.id)).toEqual(['f', 'a', 'b', 'c', 'd']);
		expect(result.hidden.map((row) => row.id)).toEqual(['e', 'g']);
	});

	it('preserves the cap and reports attention hidden beyond it', () => {
		const result = selectAgentRows(
			AGENTS,
			{ a: 'done', b: 'done', c: 'done', d: 'done', e: 'done', f: 'done', g: 'active' },
			{ a: { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true }, b: { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true }, c: { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true }, d: { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true }, e: { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true }, f: { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true }, g: { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true } },
			null,
			false,
		);

		expect(result.visible.map((row) => row.id)).toEqual(['a', 'b', 'c', 'd', 'e']);
		expect(result.hidden.map((row) => row.id)).toEqual(['f', 'g']);
		expect(result.hiddenUnread).toBe(2);
		expect(result.hiddenWorking).toBe(1);
	});

	it('keeps the selected Agent visible without changing alphabetical row order', () => {
		const result = selectAgentRows(AGENTS, {}, {}, 'g', false);

		expect(result.visible.map((row) => row.id)).toEqual(['a', 'b', 'c', 'd', 'g']);
	});

	it('keeps the selected Agent visible when working Agents exceed the cap', () => {
		const result = selectAgentRows(
			AGENTS,
			{ a: 'active', b: 'active', c: 'active', d: 'active', e: 'active', f: 'active' },
			{},
			'g',
			false,
		);

		expect(result.visible.map((row) => row.id)).toEqual(['a', 'b', 'c', 'd', 'g']);
		expect(result.hidden.map((row) => row.id)).toEqual(['e', 'f']);
		expect(result.hiddenWorking).toBe(2);
	});

	it('returns every Agent alphabetically when expanded', () => {
		const shuffled = [AGENTS[6], AGENTS[2], AGENTS[4], AGENTS[0], AGENTS[5], AGENTS[1], AGENTS[3]];
		const result = selectAgentRows(shuffled, { g: 'done' }, {}, null, true);

		expect(result.visible.map((row) => row.id)).toEqual(['a', 'b', 'c', 'd', 'e', 'f', 'g']);
		expect(result.hidden).toEqual([]);
	});
});
