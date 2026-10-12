import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { settleHeight } from './settleHeight';

let height = 300;
let notify: () => void = () => {};
const animate = vi.fn();
let finish: () => void = () => {};

beforeEach(() => {
	height = 300;
	animate.mockReset();
	animate.mockImplementation(() => ({
		addEventListener: (type: string, listener: () => void) => { if (type === 'finish') finish = listener; },
		cancel: vi.fn(),
	}));
	vi.stubGlobal('ResizeObserver', class {
		constructor(callback: () => void) { notify = callback; }
		observe() {}
		disconnect() {}
	});
});
afterEach(() => vi.unstubAllGlobals());

function panel() {
	const node = document.createElement('div');
	node.getBoundingClientRect = () => ({ height }) as DOMRect;
	node.animate = animate as unknown as typeof node.animate;
	return node;
}

describe('settleHeight', () => {
	it('eases a content-driven height change from the old height to the new one', () => {
		const node = panel();
		settleHeight(node, { enabled: true });
		height = 200;
		notify();
		expect(animate).toHaveBeenCalledTimes(1);
		const frames = animate.mock.calls[0][0] as Array<{ height: string }>;
		expect(frames.map((frame) => frame.height)).toEqual(['300px', '200px']);
	});

	it('cuts when disabled, as under reduced motion or a drag', () => {
		const node = panel();
		const action = settleHeight(node, { enabled: true });
		action.update({ enabled: false });
		height = 200;
		notify();
		expect(animate).not.toHaveBeenCalled();
	});

	it('leaves a detent move to the max-height transition', () => {
		const node = panel();
		settleHeight(node, { enabled: true });
		node.dispatchEvent(Object.assign(new Event('transitionrun'), { propertyName: 'max-height' }));
		height = 150;
		notify();
		expect(animate).not.toHaveBeenCalled();
	});

	it('eases on from where it stopped when the content changed again mid-way', () => {
		const node = panel();
		settleHeight(node, { enabled: true });
		height = 200;
		notify();
		height = 120;
		notify();
		expect(animate).toHaveBeenCalledTimes(1);
		finish();
		expect(animate).toHaveBeenCalledTimes(2);
		expect((animate.mock.calls[1][0] as Array<{ height: string }>).map((frame) => frame.height)).toEqual(['200px', '120px']);
	});
});
