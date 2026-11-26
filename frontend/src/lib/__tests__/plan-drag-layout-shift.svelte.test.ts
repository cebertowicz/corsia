import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import { positionBeside } from '$lib/components/plans/planReorder';
import type { OperatorNode } from '$lib/api/plans';
import { node } from './fixtures/planTreeNode';
import { withLiveFolds } from './fixtures/planFolds';

let component: Record<string, unknown> | undefined;
afterEach(() => { if (component) unmount(component); component = undefined; document.body.innerHTML = ''; vi.restoreAllMocks(); vi.unstubAllGlobals(); });

/** Lays the tree out at `shift.top` (what sits above it, such as the refusal banner), with 40px rows in DOM order. */
function layout(host: HTMLElement, shift: { top: number }) {
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		if (this.classList.contains('plan-tree')) return new DOMRect(0, shift.top, 300, 400);
		const at = [...host.querySelectorAll('.tree-row')].indexOf(this);
		return new DOMRect(0, shift.top + Math.max(at, 0) * 40, 300, 40);
	});
	vi.stubGlobal('DOMMatrixReadOnly', class { m42 = 0; });
	const frames = new Map<number, FrameRequestCallback>(); let id = 0;
	vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { frames.set(++id, cb); return id; });
	vi.stubGlobal('cancelAnimationFrame', (key: number) => frames.delete(key));
	return () => { const pending = [...frames.values()]; frames.clear(); pending.forEach((cb) => cb(0)); flushSync(); };
}

const rowOf = (host: HTMLElement, id: string) => host.querySelector<HTMLElement>(`.tree-row[data-node-id="${id}"]`)!;
const pointer = (row: HTMLElement, type: string, y: number) => row.dispatchEvent(new PointerEvent(type, { bubbles: true, button: 0, pointerId: 1, pointerType: 'mouse', clientY: y }));

it('keeps the drop and the lifted row true when what sits above the tree goes away mid-drag', async () => {
	const host = document.createElement('div'); document.body.appendChild(host);
	const nodes = Array.from({ length: 5 }, (_, i) => node({ id: `n${i}`, user_actions: ['reorder'] }));
	const apply = vi.fn();
	const props = $state({ nodes, onUserAction: apply });
	component = mount(PlanTree, { target: host, props: withLiveFolds(props) }); flushSync();
	const shift = { top: 40 };
	const frame = layout(host, shift);
	const row = rowOf(host, 'n0');
	pointer(row, 'pointerdown', 60); pointer(row, 'pointermove', 160); frame();
	expect(row.style.transform).toBe('translateY(100px)');
	// The banner that measured above the tree is gone: everything moves up by its height.
	shift.top = 0;
	pointer(row, 'pointermove', 115); frame();
	expect(row.style.transform).toBe('translateY(95px)');
	pointer(row, 'pointerup', 115);
	await tick(); await tick(); await tick();
	expect(apply.mock.calls[0]).toContain(positionBeside(nodes, 'n0', 'n2', false));
});

it('measures again when a plan update adds a row during a drag', () => {
	const host = document.createElement('div'); document.body.appendChild(host);
	const nodes: OperatorNode[] = Array.from({ length: 5 }, (_, i) => node({ id: `n${i}`, user_actions: ['reorder'] }));
	const props = $state({ nodes, onUserAction: vi.fn() });
	component = mount(PlanTree, { target: host, props: withLiveFolds(props) }); flushSync();
	const frame = layout(host, { top: 0 });
	const row = rowOf(host, 'n4');
	pointer(row, 'pointerdown', 180); pointer(row, 'pointermove', 100); frame();
	expect(row.style.transform).toBe('translateY(-80px)');
	props.nodes = [node({ id: 'new', user_actions: ['reorder'] }), ...nodes]; flushSync();
	frame();
	// n4 now lays out at 200; it still sits 20px above the pointer, as it was grabbed.
	expect(rowOf(host, 'n4').style.transform).toBe('translateY(-120px)');
	pointer(row, 'pointercancel', 100);
});
