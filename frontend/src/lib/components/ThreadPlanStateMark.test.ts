import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ThreadPlanStateMark from './ThreadPlanStateMark.svelte';
import { threadPlanStates } from '$lib/stores/threadPlanStates';
import { activeThreadId } from '$lib/stores/threads';
import type { ThreadAttentionItem, ThreadPlanStateEntry } from '$lib/api/plans';
import * as productEvents from '$lib/product_events';
import { openAtDivider } from '$lib/stores/threadUpdateCards';
import { get } from 'svelte/store';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const decision = { kind: 'decision', title: 'Choose a model', node_id: 'node-1' } as const;
const review = { kind: 'review', title: 'Review the result', node_id: 'node-r' } as const;
const hold = { kind: 'hold', title: 'Choose a direction', node_id: 'node-h' } as const;
const access = { kind: 'access', title: 'Read diagnostics', node_id: null } as const;
const source = readFileSync(resolve(__dirname, 'ThreadPlanStateMark.svelte'), 'utf8');

function render(entry: ThreadPlanStateEntry | null, onOpenAttention = vi.fn()): ReturnType<typeof vi.fn> {
	threadPlanStates.set(entry ? { 'thread-1': entry } : {});
	component = mount(ThreadPlanStateMark, { target: host, props: { threadId: 'thread-1', onOpenAttention } });
	flushSync();
	return onOpenAttention;
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	threadPlanStates.set({});
	activeThreadId.set(null);
	vi.unstubAllGlobals();
});

function renderUpdate(entry: ThreadPlanStateEntry): ReturnType<typeof vi.fn> {
	const onOpenThread = vi.fn();
	threadPlanStates.set({ 'thread-1': entry });
	component = mount(ThreadPlanStateMark, { target: host, props: { threadId: 'thread-1', onOpenThread } });
	flushSync();
	return onOpenThread;
}

describe('ThreadPlanStateMark update dot', () => {
	const updated = { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true } as const;

	it('names the dot and opens the thread by pointer or keyboard', () => {
		const onOpenThread = renderUpdate({ ...updated, attention_items: [] });
		const dot = host.querySelector<HTMLElement>('.update-dot');
		expect(dot?.getAttribute('role')).toBe('button');
		expect(dot?.getAttribute('aria-label')).toBe('Update since your last visit');
		expect(dot?.textContent).toBe('');

		dot?.click();
		dot?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
		dot?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
		expect(onOpenThread).toHaveBeenCalledTimes(2);
		expect(onOpenThread).toHaveBeenCalledWith('thread-1');
	});

	it('asks the transcript to open at the divider only after the thread opens', () => {
		openAtDivider.set(null);
		const onOpenThread = vi.fn(() => expect(get(openAtDivider)).toBeNull());
		threadPlanStates.set({ 'thread-1': { ...updated, attention_items: [] } });
		component = mount(ThreadPlanStateMark, { target: host, props: { threadId: 'thread-1', onOpenThread } });
		flushSync();

		host.querySelector<HTMLElement>('.update-dot')?.click();

		expect(onOpenThread).toHaveBeenCalledOnce();
		expect(get(openAtDivider)).toBe('thread-1');
		openAtDivider.set(null);
	});

	it('emits one sidebar.update_open success per action, with no ids or titles', () => {
		const spy = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		const onOpenThread = renderUpdate({ ...updated, attention_items: [] });
		const dot = host.querySelector<HTMLElement>('.update-dot');

		dot?.click();
		dot?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
		expect(spy.mock.calls).toEqual([['sidebar', 'update_open', { outcome: 'succeeded' }]]);
		dot?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
		expect(spy).toHaveBeenCalledTimes(2);
		expect(onOpenThread).toHaveBeenCalledTimes(2);
		spy.mockRestore();
		openAtDivider.set(null);
	});

	it('reports a declined open without claiming success', () => {
		const spy = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		openAtDivider.set('thread-1');
		const onOpenThread = vi.fn(() => false);
		threadPlanStates.set({ 'thread-1': { ...updated, attention_items: [] } });
		component = mount(ThreadPlanStateMark, { target: host, props: { threadId: 'thread-1', onOpenThread } });
		flushSync();

		host.querySelector<HTMLElement>('.update-dot')?.click();

		expect(get(openAtDivider)).toBeNull();
		expect(spy.mock.calls).toEqual([['sidebar', 'update_open', { outcome: 'refused', reason: 'not_opened' }]]);
		spy.mockRestore();
	});

	it('waits for an asynchronous open before reporting success', async () => {
		const spy = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		let finishOpen: (() => void) | undefined;
		const onOpenThread = vi.fn(() => new Promise<void>((resolve) => { finishOpen = resolve; }));
		threadPlanStates.set({ 'thread-1': { ...updated, attention_items: [] } });
		component = mount(ThreadPlanStateMark, { target: host, props: { threadId: 'thread-1', onOpenThread } });
		flushSync();

		host.querySelector<HTMLElement>('.update-dot')?.click();
		expect(spy).not.toHaveBeenCalled();
		finishOpen?.();
		await Promise.resolve();

		expect(spy.mock.calls).toEqual([['sidebar', 'update_open', { outcome: 'succeeded' }]]);
		spy.mockRestore();
	});

	it('reports a rejected asynchronous open once as failed and keeps it visible', async () => {
		const spy = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		const report = vi.fn();
		vi.stubGlobal('reportError', report);
		const failure = new Error('secret async failure text');
		let failOpen: ((error: Error) => void) | undefined;
		openAtDivider.set('thread-1');
		const onOpenThread = vi.fn(() => new Promise<void>((_, reject) => { failOpen = reject; }));
		threadPlanStates.set({ 'thread-1': { ...updated, attention_items: [] } });
		component = mount(ThreadPlanStateMark, { target: host, props: { threadId: 'thread-1', onOpenThread } });
		flushSync();

		host.querySelector<HTMLElement>('.update-dot')?.click();
		failOpen?.(failure);
		await Promise.resolve();

		expect(report).toHaveBeenCalledWith(failure);
		expect(get(openAtDivider)).toBeNull();
		expect(spy.mock.calls).toEqual([['sidebar', 'update_open', { outcome: 'failed', reason: 'open_failed' }]]);
		expect(JSON.stringify(spy.mock.calls)).not.toContain('secret');
		vi.unstubAllGlobals();
		spy.mockRestore();
	});

	it('reports a thrown open callback once as failed and still throws it', () => {
		const spy = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		openAtDivider.set(null);
		const failure = new Error('secret failure text');
		const onOpenThread = vi.fn(() => { throw failure; });
		threadPlanStates.set({ 'thread-1': { ...updated, attention_items: [] } });
		component = mount(ThreadPlanStateMark, { target: host, props: { threadId: 'thread-1', onOpenThread } });
		flushSync();
		const dot = host.querySelector<HTMLElement>('.update-dot')!;
		const thrown: unknown[] = [];
		const capture = (event: ErrorEvent) => { thrown.push(event.error); event.preventDefault(); };
		window.addEventListener('error', capture);

		dot.click();
		window.removeEventListener('error', capture);

		expect(get(openAtDivider)).toBeNull();
		expect(thrown).toEqual([failure]);
		expect(spy.mock.calls).toEqual([['sidebar', 'update_open', { outcome: 'failed', reason: 'open_failed' }]]);
		expect(JSON.stringify(spy.mock.calls)).not.toContain('secret');
		spy.mockRestore();
		openAtDivider.set(null);
	});

	it('yields to what the user owes and to Failed', () => {
		renderUpdate({ ...updated, state: 'needs_input', attention_items: [decision] });
		expect(host.querySelector('.update-dot')).toBeNull();
		expect(host.querySelector('.plan-state')?.textContent).toBe('Decide');
	});

	it('shows no dot without an update or on the open thread', () => {
		renderUpdate({ ...updated, attention_items: [], has_update: false });
		expect(host.querySelector('.update-dot')).toBeNull();
		unmount(component!);
		activeThreadId.set('thread-1');
		renderUpdate({ ...updated, attention_items: [] });
		expect(host.querySelector('.update-dot')).toBeNull();
	});
});

describe('ThreadPlanStateMark', () => {
	it.each([
		[{ state: 'needs_input', items: [decision] }, 'Decide'],
		[{ state: 'blocked_on_you', items: [{ kind: 'hold', title: 'Choose a direction', node_id: 'node-2' }] }, 'Needs you'],
		[{ state: 'failed', items: [{ kind: 'failed', title: 'Ship report', node_id: 'node-3' }] }, 'Failed'],
	] as const)('names %o in words', ({ state, items }, word) => {
		render({ state, label_key: null, paused: false, attention_items: [...items] });
		const mark = host.querySelector<HTMLElement>('.plan-state');
		expect(mark?.textContent).toBe(word);
		expect(mark?.classList.contains(state)).toBe(true);
	});

	it.each([
		{ state: 'idle', label_key: null, paused: false, attention_items: [] },
		{ state: 'moving', label_key: 'working', paused: false, attention_items: [] },
		{ state: 'moving', label_key: 'waiting', paused: false, attention_items: [] },
		{ state: 'paused', label_key: 'waiting', paused: true, attention_items: [] },
		null,
	] as const)('stays quiet for %o: motion belongs to turn truth, the pause to the row-leading mark', (entry) => {
		render(entry && { ...entry, attention_items: [] });
		expect(host.innerHTML.replace(/<!---->/g, '').trim()).toBe('');
	});

	it('keeps Moving without a running Turn empty, not an arc at rest', () => {
		render({ state: 'moving', label_key: 'waiting', paused: false, attention_items: [] });
		expect(host.querySelector('svg')).toBeNull();
		expect(host.querySelector('.update-dot, .plan-state, .paused-glyph')).toBeNull();
	});

	it('names user attention and delegates keyboard activation to the sidebar overlay', () => {
		const onOpenAttention = render({
			state: 'needs_input', label_key: 'waiting', paused: false, attention_items: [decision],
		});
		const mark = host.querySelector<HTMLElement>('.plan-state')!;
		expect(mark.title).toBe('Needs input · Plan: Waiting');
		mark.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
		expect(onOpenAttention).toHaveBeenCalledWith('thread-1', 'Decide');
		expect(host.querySelector('.attention-item, .bg')).toBeNull();
	});


	it('keeps the pause separate when the user owes a review', () => {
		render({
			state: 'blocked_on_you', label_key: 'needs_you', paused: true,
			attention_items: [{ kind: 'review', title: 'Review the result', node_id: 'node-4' }],
		});
		const mark = host.querySelector<HTMLElement>('.plan-state');
		expect(mark?.textContent).toBe('Review');
		expect(mark?.title).toBe('Blocked on you · Plan: Needs you · Paused');
		expect(host.querySelector('.paused-glyph')).toBeNull();
	});

	it.each([
		[[decision], 'Decide'],
		[[decision, { kind: 'decision', title: 'Pick a plan', node_id: 'node-2' }], '2 to decide'],
		[[decision, review], '2 for you'],
		[[decision, review, hold], '3 for you'],
		[[access], 'Allow access'],
		[[hold], 'Needs you'],
	] as const)('names %o as %s', (items: readonly ThreadAttentionItem[], word) => {
		render({ state: 'needs_input', label_key: null, paused: false, attention_items: [...items] });
		expect(host.querySelector<HTMLElement>('.plan-state')?.textContent).toBe(word);
	});

	it('names several Failed items with a count', () => {
		render({
			state: 'failed', label_key: null, paused: false,
			attention_items: [{ kind: 'failed', title: 'Ship report', node_id: 'node-3' }, { kind: 'failed', title: 'Deploy', node_id: 'node-5' }],
		});
		expect(host.querySelector<HTMLElement>('.plan-state')?.textContent).toBe('2 Failed');
	});

	it('shows the existing update dot for an overdue-only idle row', () => {
		const onOpenThread = renderUpdate({
			state: 'idle', label_key: null, paused: false, attention_items: [],
			next_overdue_at: '2026-10-14T00:00:00+00:00', has_update: true,
		});
		expect(host.querySelector('.plan-state:not(.update-dot)')).toBeNull();
		const dot = host.querySelector<HTMLElement>('.update-dot')!;
		expect(dot.getAttribute('aria-label')).toBe('Update since your last visit');
		dot.click();
		dot.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
		expect(onOpenThread).toHaveBeenCalledTimes(2);
	});

	it('stays a plain word, not a control, when a your-move state carries zero items', () => {
		render({ state: 'needs_input', label_key: 'waiting', paused: false, attention_items: [] });
		const mark = host.querySelector<HTMLElement>('.plan-state');
		expect(mark?.textContent).toBe('Needs input');
		expect(mark?.getAttribute('role')).toBeNull();
		expect(mark?.classList.contains('static')).toBe(true);
	});

	it('gives the phone pointer target at least 44px each way, keeping keyboard focus', () => {
		render({ state: 'needs_input', label_key: null, paused: false, attention_items: [decision] });
		const mark = host.querySelector<HTMLElement>('.plan-state')!;

		expect(mark.getAttribute('role')).toBe('button');
		expect(mark.getAttribute('tabindex')).toBe('0');
		mark.focus();
		expect(document.activeElement).toBe(mark);
		// jsdom has no layout; the measured size is proven in a real browser at 393px.
		const phoneTarget = source.match(/@media \(max-width: 768px\)[\s\S]*\.plan-state\[role="button"\]::before\s*\{([^}]*)\}/)?.[1] ?? '';
		expect(phoneTarget).toMatch(/width:\s*max\(calc\(100% \+ 20px\), 44px\);/);
		expect(phoneTarget).toMatch(/height:\s*max\(100%, 44px\);/);
		expect(source).toMatch(/\.plan-state:focus-visible/);
	});

	it('emits a product event when the mark is opened', () => {
		const spy = vi.spyOn(productEvents, 'emitProductEvent');
		render({ state: 'needs_input', label_key: null, paused: false, attention_items: [decision] });
		host.querySelector<HTMLElement>('.plan-state')?.click();
		expect(spy).toHaveBeenCalledWith('sidebar', 'attention_opened', expect.objectContaining({ thread_id: 'thread-1', word: 'Decide' }));
	});
});
