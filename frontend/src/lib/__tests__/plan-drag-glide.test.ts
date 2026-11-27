import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import { node } from './fixtures/planTreeNode';
import { withFolds } from './fixtures/planFolds';

const motion = vi.hoisted(() => ({ capture: vi.fn(), play: vi.fn(), reduced: vi.fn() }));
vi.mock('$lib/components/plans/foldWave', async (original) => ({
	...(await original<typeof import('$lib/components/plans/foldWave')>()),
	captureFold: motion.capture, playFold: motion.play, prefersReducedMotion: motion.reduced,
}));

afterEach(() => { document.body.innerHTML = ''; vi.clearAllMocks(); vi.unstubAllGlobals(); });

it.each([false, true])('glides the dropped row unless reduced motion is %s', async (reduced) => {
	const host = document.createElement('div'); document.body.appendChild(host);
	const nodes = Array.from({ length: 3 }, (_, i) => node({ id: `n${i}`, user_actions: ['reorder'] }));
	const component = mount(PlanTree, { target: host, props: withFolds({ nodes, onUserAction: vi.fn() }) }); flushSync();
	const rows = [...host.querySelectorAll<HTMLElement>('.tree-row')];
	rows.forEach((row, i) => { row.getBoundingClientRect = () => new DOMRect(0, i * 40, 300, 40); });
	vi.stubGlobal('DOMMatrixReadOnly', class { m42 = 0; });
	const frames = new Map<number, FrameRequestCallback>(); let id = 0;
	vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { frames.set(++id, cb); return id; });
	vi.stubGlobal('cancelAnimationFrame', (key: number) => frames.delete(key));
	const frame = () => { const pending = [...frames.values()]; frames.clear(); pending.forEach((cb) => cb(0)); flushSync(); };
	const pointer = (type: string, y: number) => rows[0].dispatchEvent(new PointerEvent(type, { bubbles: true, button: 0, pointerId: 1, pointerType: 'mouse', clientY: y }));
	const snapshot = { marker: 'before-drop' };
	motion.reduced.mockReturnValue(reduced);
	motion.capture.mockImplementation(() => { expect(rows[0].style.transform).toBe('translateY(80px)'); return snapshot; });
	pointer('pointerdown', 20); pointer('pointermove', 100); frame(); pointer('pointerup', 100);
	await tick(); await tick(); await tick();
	if (reduced) { expect(motion.capture).not.toHaveBeenCalled(); expect(motion.play).not.toHaveBeenCalled(); }
	else { expect(motion.capture).toHaveBeenCalledOnce(); expect(motion.play).toHaveBeenCalledWith(expect.any(HTMLElement), snapshot); }
	unmount(component);
});
