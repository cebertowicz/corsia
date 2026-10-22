import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ThreadPlanStateEntry } from '$lib/api/plans';
import { canMarkSeen, showsWaitingGlyph, slotThreeWord, waitMarkTitle } from './threadRowMark';

const idle: ThreadPlanStateEntry = { state: 'idle', label_key: null, paused: false, attention_items: [] };
const decision = { kind: 'decision', title: 'Choose', node_id: 'n1' } as const;
const failed = { kind: 'failed', title: 'Ship', node_id: 'n2' } as const;

describe('canMarkSeen', () => {
	it('offers Mark read/unread only when the dot owns slot 3 and a seen record exists', () => {
		expect(canMarkSeen({ ...idle, opened: true, has_update: true })).toBe(true);
		expect(canMarkSeen({ ...idle, opened: true, has_update: false })).toBe(true);
		expect(canMarkSeen({ ...idle, opened: false, has_update: true })).toBe(false);
		expect(canMarkSeen(undefined)).toBe(false);
	});

	it('hides it while an owed or Failed word outranks the dot', () => {
		expect(canMarkSeen({ ...idle, state: 'needs_input', attention_items: [decision], opened: true, has_update: true }))
			.toBe(false);
		expect(canMarkSeen({ ...idle, state: 'failed', attention_items: [failed], opened: true, has_update: true }))
			.toBe(false);
	});
});

describe('slotThreeWord', () => {
	it('is empty when nothing outranks the dot', () => {
		expect(slotThreeWord(idle)).toBe('');
		expect(slotThreeWord({ ...idle, paused: true })).toBe('');
	});

	it('renders an owed word even when the server has no item details', () => {
		expect(slotThreeWord({ ...idle, state: 'needs_input', attention_items: [] })).toBe('Needs input');
		expect(slotThreeWord({ ...idle, state: 'blocked_on_you', attention_items: [] })).toBe('Blocked on you');
	});

	it('counts several decisions as to decide', () => {
		expect(slotThreeWord({ ...idle, state: 'needs_input', attention_items: [decision, decision] })).toBe('2 to decide');
	});
});

describe('waiting', () => {
	// Local noon: 40 minutes later is still "today", whatever time the suite runs.
	const noon = new Date(2026, 0, 15, 12, 0).getTime();
	const wait = { label: 'CI on PR #1193', until: new Date(noon + 40 * 60_000).toISOString(), more: 0 };
	beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(noon); });
	afterEach(() => { vi.useRealTimers(); });

	it('is a glyph, never a word in slot 3', () => {
		expect(slotThreeWord({ ...idle, state: 'waiting', wait })).toBe('');
	});

	it('draws the hourglass only when no spinner shows', () => {
		expect(showsWaitingGlyph({ ...idle, state: 'waiting', wait }, false)).toBe(true);
		expect(showsWaitingGlyph({ ...idle, state: 'waiting', wait }, true)).toBe(false);
		expect(showsWaitingGlyph({ ...idle, state: 'idle' }, false)).toBe(false);
		expect(showsWaitingGlyph(undefined, false)).toBe(false);
	});

	it('titles the glyph with what it waits for and when it checks back', () => {
		expect(waitMarkTitle({ ...idle, state: 'waiting', wait })).toMatch(/^Waiting for CI on PR #1193 · checks back today at /);
		expect(waitMarkTitle({ ...idle, state: 'waiting', wait: null })).toBe('');
		expect(waitMarkTitle({ ...idle, state: 'idle', wait })).toBe('');
	});
});
