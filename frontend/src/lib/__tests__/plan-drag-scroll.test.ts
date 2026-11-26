import { flushSync, mount, unmount } from 'svelte';
import { expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import { node } from './fixtures/planTreeNode';
import { withFolds } from './fixtures/planFolds';

it('keeps the lifted row under the pointer when its container scrolls', () => {
	const host = document.createElement('div'); host.style.overflowY = 'auto'; document.body.appendChild(host);
	Object.defineProperties(host, { scrollHeight: { value: 500 }, clientHeight: { value: 200 } });
	host.getBoundingClientRect = () => new DOMRect(0, 0, 300, 200);
	const nodes = Array.from({ length: 5 }, (_, i) => node({ id: `n${i}`, user_actions: ['reorder'] }));
	const component = mount(PlanTree, { target: host, props: withFolds({ nodes, onUserAction: vi.fn() }) }); flushSync();
	const rows = [...host.querySelectorAll<HTMLElement>('.tree-row')];
	// The tree's content scrolls with its rows.
	host.querySelector<HTMLElement>('.plan-tree')!.getBoundingClientRect = () => new DOMRect(0, -host.scrollTop, 300, 200);
	rows.forEach((row, i) => { row.getBoundingClientRect = vi.fn(() => new DOMRect(0, i * 40 - host.scrollTop, 300, 40)); });
	vi.stubGlobal('DOMMatrixReadOnly', class { m42 = 0; });
	const frames = new Map<number, FrameRequestCallback>(); let id = 0;
	vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { frames.set(++id, cb); return id; });
	vi.stubGlobal('cancelAnimationFrame', (key: number) => frames.delete(key));
	const frame = () => { const pending = [...frames.values()]; frames.clear(); pending.forEach((cb) => cb(0)); flushSync(); };
	const pointer = (type: string, y: number) => rows[0].dispatchEvent(new PointerEvent(type, { bubbles: true, button: 0, pointerId: 1, pointerType: 'mouse', clientY: y }));
	pointer('pointerdown', 20); pointer('pointermove', 80); frame();
	expect(rows[0].style.transform).toBe('translateY(60px)');
	host.scrollTop = 30; frame();
	expect(rows[0].style.transform).toBe('translateY(90px)');
	expect(rows.reduce((n, row) => n + vi.mocked(row.getBoundingClientRect).mock.calls.length, 0)).toBe(5);
	pointer('pointercancel', 80); unmount(component); host.remove(); vi.unstubAllGlobals();
});
