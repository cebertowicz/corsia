import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import { positionBeside } from '$lib/components/plans/planReorder';
import { node } from './fixtures/planTreeNode';
import { withFolds } from './fixtures/planFolds';

let component: Record<string, unknown> | undefined;
afterEach(() => { if (component) unmount(component); document.body.innerHTML = ''; vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('tracks the pointer, coalesces moves and preserves the old drop position on 100 rows', async () => {
	const host = document.createElement('div'); document.body.appendChild(host);
	const nodes = Array.from({ length: 100 }, (_, i) => node({ id: `n${i}`, title: `Task ${i}`, user_actions: ['reorder'] }));
	const apply = vi.fn();
	component = mount(PlanTree, { target: host, props: withFolds({ nodes, onUserAction: apply }) }); flushSync();
	const rows = [...host.querySelectorAll<HTMLElement>('.tree-row')]; expect(rows).toHaveLength(100);
	rows.forEach((row, i) => { row.getBoundingClientRect = vi.fn(() => new DOMRect(0, i * 40, 300, 40)); });
	vi.stubGlobal('DOMMatrixReadOnly', class { m42 = 0; });
	const frames = new Map<number, FrameRequestCallback>(); let frameId = 0;
	vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { frames.set(++frameId, cb); return frameId; });
	vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id));
	const frame = () => { const pending = [...frames.values()]; frames.clear(); pending.forEach((cb) => cb(0)); flushSync(); };
	const pointer = (type: string, y: number) => rows[0].dispatchEvent(new PointerEvent(type, { bubbles: true, button: 0, pointerId: 1, pointerType: 'mouse', clientY: y }));
	pointer('pointerdown', 20);
	for (let i = 0; i < 10; i++) pointer('pointermove', 55 + i * 20);
	expect(frames.size).toBe(1);
	frame();
	const reads = rows.reduce((n, row) => n + vi.mocked(row.getBoundingClientRect).mock.calls.length, 0);
	expect(reads).toBe(100);
	expect(rows[0].style.transform).toBe('translateY(215px)');
	expect(rows[0].classList.contains('drag-lifted')).toBe(true);
	for (let i = 0; i < 10; i++) { pointer('pointermove', 55 + i * 20); frame(); }
	expect(rows.reduce((n, row) => n + vi.mocked(row.getBoundingClientRect).mock.calls.length, 0)).toBe(100);
	// The former algorithm chooses the nearest untransformed slot centre, then positionBeside.
	const y = 275;
	const nearest = nodes.reduce((best, _, i) => Math.abs(y - (i * 40 + 20)) < Math.abs(y - (best * 40 + 20)) ? i : best, 0);
	const expected = positionBeside(nodes, nodes[0].id, nodes[nearest].id, y < nearest * 40 + 20);
	// A release before the pending frame still saves the final move.
	pointer('pointermove', y); pointer('pointerup', y);
	expect(apply).not.toHaveBeenCalled();
	frame(); await tick(); await tick(); await tick();
	expect(apply).toHaveBeenCalled();
	expect(apply.mock.calls[0]).toContain(expected);
});
