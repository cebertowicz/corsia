import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import type { OperatorNode } from '$lib/api/plans';
import { node } from './fixtures/planTreeNode';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	document.body.querySelectorAll('[role="menu"]').forEach((menu) => menu.remove());
	vi.useRealTimers();
});

async function settle() {
	await tick();
	flushSync();
}

type Handler = (
	target: OperatorNode, action: string, reason?: string,
) => Promise<string | null> | void;

function mountTree(over: Partial<OperatorNode>, onUserAction?: Handler) {
	component = mount(PlanTree, {
		target: host,
		props: { nodes: [node({ id: 'task', title: 'Write the report', ...over })], onUserAction },
	});
	flushSync();
	return host.querySelector('.tree-row') as HTMLElement;
}

const menu = () => document.body.querySelector('[role="menu"]') as HTMLElement | null;
const labels = () =>
	[...(menu()?.querySelectorAll('[role="menuitem"]') ?? [])].map((item) => item.textContent?.trim());

function rightClick(row: HTMLElement) {
	row.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 40, clientY: 30 }));
}

describe('PlanTree task context menu', () => {
	it('opens on right-click and lists what the row state allows', async () => {
		const row = mountTree({ user_actions: ['pause_node', 'close_node'] }, vi.fn());
		expect(row.querySelector('.row-body')!.getAttribute('aria-haspopup')).toBe('menu');

		rightClick(row);
		await settle();

		expect(labels()).toEqual(['Pause', 'Won’t do']);
	});

	it('focuses the first item without scrolling an ancestor pane out from under the row', async () => {
		// The menu is `position: fixed` but still a DOM descendant of the
		// scrollable pane. Focusing it without `preventScroll` makes the
		// browser scroll that ancestor to "reveal" an already-visible fixed
		// element, sliding the whole tree out from behind the menu.
		const row = mountTree({ user_actions: ['pause_node', 'close_node'] }, vi.fn());
		const focusSpy = vi.spyOn(HTMLElement.prototype, 'focus');

		rightClick(row);
		await settle();

		const first = menu()!.querySelector('[role="menuitem"]') as HTMLButtonElement;
		expect(document.activeElement).toBe(first);
		expect(focusSpy).toHaveBeenCalledWith({ preventScroll: true });
		expect(host.querySelectorAll(`[id="${row.id}"]`)).toHaveLength(1);
		expect(host.contains(row)).toBe(true);
	});

	it('renders the menu as a direct child of document.body, not inside the pane', async () => {
		// `aside.pane` uses `backdrop-filter`, which establishes a CSS
		// containing block for `position: fixed` descendants — a `fixed`
		// menu inside it resolves its coordinates against the pane's box,
		// not the viewport, and can render off-screen. Portaling to
		// `document.body` sidesteps that ancestor entirely.
		const row = mountTree({ user_actions: ['pause_node', 'close_node'] }, vi.fn());

		rightClick(row);
		await settle();

		const menuRoot = menu()!;
		expect(menuRoot.parentElement).toBe(document.body);
		expect(host.contains(menuRoot)).toBe(false);

		menuRoot.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
		await settle();
		expect(document.body.querySelector('[role="menu"]')).toBeNull();
	});

	it('offers Resume on the user’s own pause and Mark done on their own task', async () => {
		const row = mountTree({ user_actions: ['mark_done', 'resume_node', 'close_node'] }, vi.fn());

		rightClick(row);
		await settle();

		expect(labels()).toEqual(['Mark done', 'Resume', 'Won’t do']);
	});

	it('does not open, and leaves the browser menu, where nothing applies', async () => {
		const row = mountTree({ user_actions: [] }, vi.fn());
		const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });

		row.dispatchEvent(event);
		await settle();

		expect(menu()).toBeNull();
		expect(event.defaultPrevented).toBe(false);
		expect(row.querySelector('.row-body')!.getAttribute('aria-haspopup')).toBeNull();
	});

	it('does not open where the plan cannot be changed', async () => {
		const row = mountTree({ user_actions: ['pause_node', 'close_node'] });

		rightClick(row);
		await settle();

		expect(menu()).toBeNull();
	});

	it('opens from the keyboard with Shift+F10 and the context-menu key, and Escape closes it', async () => {
		const row = mountTree({ user_actions: ['pause_node', 'close_node'] }, vi.fn());
		const toggle = row.querySelector('.row-body') as HTMLButtonElement;
		toggle.focus();

		toggle.dispatchEvent(new KeyboardEvent('keydown', { key: 'F10', shiftKey: true, bubbles: true }));
		await settle();
		expect(labels()).toEqual(['Pause', 'Won’t do']);

		menu()!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
		await settle();
		expect(menu()).toBeNull();
		expect(document.activeElement).toBe(toggle);

		toggle.dispatchEvent(new KeyboardEvent('keydown', { key: 'ContextMenu', bubbles: true }));
		await settle();
		expect(menu()).not.toBeNull();
	});

	it('reports the picked action and closes once it succeeds', async () => {
		const calls: string[] = [];
		const row = mountTree({ user_actions: ['pause_node', 'close_node'] }, async (target, action) => {
			calls.push(`${target.id}:${action}`);
			return null;
		});

		rightClick(row);
		await settle();
		(menu()!.querySelector('[role="menuitem"]') as HTMLButtonElement).click();
		await settle();

		expect(calls).toEqual(['task:pause_node']);
		expect(menu()).toBeNull();
	});

	it('shows the server’s own refusal in the menu and stays open', async () => {
		const row = mountTree({ user_actions: ['pause_node', 'close_node'] }, async () =>
			'The agent set this task aside, and only it resumes it.');

		rightClick(row);
		await settle();
		(menu()!.querySelector('[role="menuitem"]') as HTMLButtonElement).click();
		await settle();

		expect(menu()!.querySelector('[role="alert"]')!.textContent).toContain('only it resumes it');
	});

	it('stops a long-press from also selecting text on touch screens', () => {
		const row = mountTree({ user_actions: ['pause_node', 'close_node'] }, vi.fn());

		expect(row.classList.contains('has-menu')).toBe(true);
	});

	it('acts on Won’t do in one tap, with no reason prompt in front of it', async () => {
		const calls: string[] = [];
		const row = mountTree({ user_actions: ['pause_node', 'close_node'] }, async (_target, action) => {
			calls.push(action);
			return null;
		});

		rightClick(row);
		await settle();
		expect(menu()!.querySelector('input')).toBeNull();
		([...menu()!.querySelectorAll('[role="menuitem"]')][1] as HTMLButtonElement).click();
		await settle();

		expect(calls).toEqual(['close_node']);
		expect(menu()).toBeNull();
	});

	it('says settling stops the worker too when the row has a live worker', async () => {
		const row = mountTree({
			user_actions: ['pause_node', 'close_node'],
			child: { available: true, plan_id: 'p', thread_id: 't', progress: { done: 0, total: 1 } } as never,
		}, vi.fn());

		rightClick(row);
		await settle();

		expect(menu()!.textContent).toContain('Mark done or Won’t do stops its worker too.');
	});

	it('opens on a 500 ms touch long-press and swallows the tap that follows', async () => {
		vi.useFakeTimers();
		const row = mountTree({ user_actions: ['pause_node', 'close_node'] }, vi.fn());
		const body = row.querySelector('.row-body') as HTMLElement;
		const toggled = vi.fn();
		body.addEventListener('click', toggled);

		row.dispatchEvent(new PointerEvent('pointerdown', { pointerType: 'touch', bubbles: true, clientX: 10, clientY: 10 }));
		vi.advanceTimersByTime(499);
		expect(menu()).toBeNull();
		vi.advanceTimersByTime(2);
		flushSync();
		expect(labels()).toEqual(['Pause', 'Won’t do']);

		row.dispatchEvent(new PointerEvent('pointerup', { pointerType: 'touch', bubbles: true }));
		body.click();
		expect(toggled).not.toHaveBeenCalled();
	});

	it('cancels the long-press when the finger lifts early or moves', async () => {
		vi.useFakeTimers();
		const row = mountTree({ user_actions: ['pause_node', 'close_node'] }, vi.fn());

		row.dispatchEvent(new PointerEvent('pointerdown', { pointerType: 'touch', bubbles: true, clientX: 10, clientY: 10 }));
		vi.advanceTimersByTime(200);
		row.dispatchEvent(new PointerEvent('pointerup', { pointerType: 'touch', bubbles: true }));
		vi.advanceTimersByTime(600);
		expect(menu()).toBeNull();

		row.dispatchEvent(new PointerEvent('pointerdown', { pointerType: 'touch', bubbles: true, clientX: 10, clientY: 10 }));
		row.dispatchEvent(new PointerEvent('pointermove', { pointerType: 'touch', bubbles: true, clientX: 40, clientY: 10 }));
		vi.advanceTimersByTime(600);
		expect(menu()).toBeNull();
	});
});
