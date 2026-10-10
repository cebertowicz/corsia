import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { focusPlanNode, registerNodeFocus, registerPlanSurface } from '$lib/planNodeFocus';
import { activeThreadId } from '$lib/stores/threads';

const cleanups: Array<() => void> = [];
function register(off: () => void): void {
	cleanups.push(off);
}

beforeEach(() => activeThreadId.set('t1'));
afterEach(() => {
	for (const off of cleanups.splice(0)) off();
});

describe('focusPlanNode', () => {
	it('shows the plan, then calls the thread’s handler once', async () => {
		const show = vi.fn(() => true);
		const focus = vi.fn();
		register(registerPlanSurface(show));
		register(registerNodeFocus('t1', focus));

		await focusPlanNode('t1', 'node-1');

		expect(show).toHaveBeenCalledWith('t1');
		expect(focus).toHaveBeenCalledOnce();
		expect(focus).toHaveBeenCalledWith('node-1', null);
	});

	it('hands a nested subagent-plan node over with the step that nests it', async () => {
		const focus = vi.fn();
		register(registerPlanSurface(() => true));
		register(registerNodeFocus('t1', focus));

		await focusPlanNode('t1', 'sub', 'spawn');

		expect(focus).toHaveBeenCalledWith('sub', 'spawn');
	});

	it('drops the request when no surface can show the plan', async () => {
		const focus = vi.fn();
		register(registerPlanSurface(() => false));
		register(registerNodeFocus('t1', focus));

		await focusPlanNode('t1', 'node-1');

		expect(focus).not.toHaveBeenCalled();
	});

	it('drops the request when the user has left the thread by the time the pane mounts', async () => {
		const focus = vi.fn();
		register(registerPlanSurface(() => {
			activeThreadId.set('t2');
			return true;
		}));
		register(registerNodeFocus('t1', focus));

		await focusPlanNode('t1', 'node-1');

		expect(focus).not.toHaveBeenCalled();
	});

	it('keeps nothing for a handler that registers later', async () => {
		register(registerPlanSurface(() => true));
		await focusPlanNode('t1', 'node-1');

		const focus = vi.fn();
		register(registerNodeFocus('t1', focus));
		await Promise.resolve();

		expect(focus).not.toHaveBeenCalled();
	});

	it('does nothing for an item with no node', async () => {
		const show = vi.fn(() => true);
		register(registerPlanSurface(show));

		await focusPlanNode('t1', null);

		expect(show).not.toHaveBeenCalled();
	});

	it('lets a stale unregister leave a newer pane’s handler in place', async () => {
		register(registerPlanSurface(() => true));
		const offOld = registerNodeFocus('t1', vi.fn());
		const focus = vi.fn();
		register(registerNodeFocus('t1', focus));
		offOld();

		await focusPlanNode('t1', 'node-1');

		expect(focus).toHaveBeenCalledWith('node-1', null);
	});
});
