import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { settleHeight } from './settleHeight';

let height = 300;
let notify: () => void = () => {};
let noteAdditions: (records: MutationRecord[]) => void = () => {};
const animate = vi.fn();
const revealAnimate = vi.fn();
let originalElementAnimate: PropertyDescriptor | undefined;
let finish: () => void = () => {};
const cancelHeight = vi.fn();

beforeEach(() => {
	height = 300;
	originalElementAnimate = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'animate');
	Object.defineProperty(HTMLElement.prototype, 'animate', {
		configurable: true,
		writable: true,
		value: revealAnimate,
	});
	animate.mockReset();
	revealAnimate.mockReset();
	cancelHeight.mockReset();
	revealAnimate.mockImplementation(() => ({ addEventListener: vi.fn(), cancel: vi.fn() }));
	animate.mockImplementation(() => ({
		addEventListener: (type: string, listener: () => void) => { if (type === 'finish') finish = listener; },
		removeEventListener: vi.fn(),
		cancel: cancelHeight,
	}));
	vi.stubGlobal('ResizeObserver', class {
		constructor(callback: () => void) { notify = callback; }
		observe() {}
		disconnect() {}
	});
	vi.stubGlobal('MutationObserver', class {
		constructor(callback: (records: MutationRecord[]) => void) { noteAdditions = callback; }
		observe() {}
		disconnect() {}
	});
});
afterEach(() => {
	if (originalElementAnimate) Object.defineProperty(HTMLElement.prototype, 'animate', originalElementAnimate);
	else delete (HTMLElement.prototype as { animate?: typeof HTMLElement.prototype.animate }).animate;
	vi.unstubAllGlobals();
});

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
		expect(animate.mock.calls[0][1]).toMatchObject({ fill: 'both' });
	});

	it('releases the held end height once the ease finishes, so later content sizes the panel', () => {
		const node = panel();
		settleHeight(node, { enabled: true });
		height = 200;
		notify();
		expect(cancelHeight).not.toHaveBeenCalled();
		finish();
		expect(cancelHeight).toHaveBeenCalledTimes(1);
		height = 260;
		notify();
		expect(animate).toHaveBeenCalledTimes(2);
		expect((animate.mock.calls[1][0] as Array<{ height: string }>).map((frame) => frame.height)).toEqual(['200px', '260px']);
	});

	it('does not hold insertions made outside reduced motion', () => {
		const node = panel();
		const added = document.createElement('footer');
		const addedAnimate = vi.fn(() => ({ addEventListener: vi.fn(), cancel: vi.fn() }));
		added.animate = addedAnimate as unknown as typeof added.animate;
		const action = settleHeight(node, { enabled: true });
		node.append(added);
		noteAdditions([{ addedNodes: [added] } as unknown as MutationRecord]);
		action.update({ enabled: true, reduced: true });
		height = 400;
		notify();
		expect(addedAnimate).not.toHaveBeenCalled();
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

	it('leaves content that eases its own height to carry the panel, frame by frame', () => {
		const node = panel();
		node.getAnimations = (() => [{ playState: 'running', effect: { getKeyframes: () => [{ height: '0px', opacity: 0 }] } }]) as unknown as typeof node.getAnimations;
		settleHeight(node, { enabled: true });
		height = 200;
		notify();
		expect(animate).not.toHaveBeenCalled();
	});

	it('still eases while only a fade runs inside the panel', () => {
		const node = panel();
		node.getAnimations = (() => [{ playState: 'running', effect: { getKeyframes: () => [{ opacity: 0 }, { opacity: 1 }] } }]) as unknown as typeof node.getAnimations;
		settleHeight(node, { enabled: true });
		height = 200;
		notify();
		expect(animate).toHaveBeenCalledTimes(1);
	});

	it('keeps existing children at full opacity on a reduced-motion height change', () => {
		const node = panel();
		const body = document.createElement('section');
		const bodyAnimate = vi.fn();
		body.animate = bodyAnimate as unknown as typeof body.animate;
		node.append(body);
		settleHeight(node, { enabled: true, reduced: true });
		height = 200;
		notify();
		expect(animate).not.toHaveBeenCalled();
		expect(bodyAnimate).not.toHaveBeenCalled();
	});

	it('fades only the newly revealed region after a reduced-motion size cut', () => {
		const node = panel();
		const existing = document.createElement('section');
		const existingAnimate = vi.fn();
		existing.animate = existingAnimate as unknown as typeof existing.animate;
		node.append(existing);
		settleHeight(node, { enabled: true, reduced: true });
		height = 400;
		notify();
		expect(existingAnimate).not.toHaveBeenCalled();
		expect(revealAnimate).toHaveBeenCalledTimes(1);
		expect(revealAnimate.mock.calls[0][0]).toEqual([{ opacity: 1 }, { opacity: 0 }]);
		expect((node.querySelector('[data-settle-height-reveal]') as HTMLElement).style.top).toBe('300px');
	});

	it('fades only newly added content after a reduced-motion detent resize', () => {
		const node = panel();
		const existing = document.createElement('section');
		const added = document.createElement('footer');
		const existingAnimate = vi.fn();
		const addedAnimate = vi.fn(() => ({ addEventListener: vi.fn(), cancel: vi.fn() }));
		existing.animate = existingAnimate as unknown as typeof existing.animate;
		added.animate = addedAnimate as unknown as typeof added.animate;
		node.append(existing);
		settleHeight(node, { enabled: true, reduced: true });
		node.append(added);
		noteAdditions([{ addedNodes: [added] } as unknown as MutationRecord]);
		node.dispatchEvent(Object.assign(new Event('transitionrun'), { propertyName: 'max-height' }));
		node.dispatchEvent(Object.assign(new Event('transitionend'), { propertyName: 'max-height' }));
		expect(existingAnimate).not.toHaveBeenCalled();
		expect(addedAnimate).toHaveBeenCalledTimes(1);
		expect((addedAnimate.mock.calls[0][0] as Array<{ opacity: number }>).map((frame) => frame.opacity)).toEqual([0, 1]);
	});

	it('hides newly inserted reduced-motion content before starting its fade', () => {
		const node = panel();
		const added = document.createElement('footer');
		let preFadeOpacity: { inline: string; computed: string } | undefined;
		let finishFade: (() => void) | undefined;
		added.animate = vi.fn(() => {
			preFadeOpacity = { inline: added.style.opacity, computed: getComputedStyle(added).opacity };
			return {
				addEventListener: (type: string, listener: () => void) => { if (type === 'finish') finishFade = listener; },
				cancel: vi.fn(),
			};
		}) as unknown as typeof added.animate;
		settleHeight(node, { enabled: true, reduced: true });

		node.append(added);
		noteAdditions([{ addedNodes: [added] } as unknown as MutationRecord]);

		expect(added.animate).toHaveBeenCalledTimes(1);
		expect(preFadeOpacity).toEqual({ inline: '0', computed: '0' });
		expect((added.animate as ReturnType<typeof vi.fn>).mock.calls[0][0]).toEqual([{ opacity: 0 }, { opacity: 1 }]);
		finishFade?.();
		expect(added.style.opacity).toBe('');
	});

	it('fades a later size-only reveal after an insertion that caused no resize', () => {
		const node = panel();
		const added = document.createElement('footer');
		let finishFade: (() => void) | undefined;
		added.animate = vi.fn(() => ({
			addEventListener: (type: string, listener: () => void) => { if (type === 'finish') finishFade = listener; },
			cancel: vi.fn(),
		})) as unknown as typeof added.animate;
		settleHeight(node, { enabled: true, reduced: true });

		node.append(added);
		noteAdditions([{ addedNodes: [added] } as unknown as MutationRecord]);
		finishFade?.();
		height = 400;
		notify();

		expect(revealAnimate).toHaveBeenCalledTimes(1);
		expect(revealAnimate.mock.calls[0][0]).toEqual([{ opacity: 1 }, { opacity: 0 }]);
	});

	it('leaves a subtree-owned reduced-motion opacity crossfade alone', () => {
		const node = panel();
		const added = document.createElement('footer');
		const addedAnimate = vi.fn();
		added.animate = addedAnimate as unknown as typeof added.animate;
		added.getAnimations = (() => [{ playState: 'running', effect: { getKeyframes: () => [{ opacity: 0 }, { opacity: 1 }] } }]) as unknown as typeof added.getAnimations;
		settleHeight(node, { enabled: true, reduced: true });
		node.append(added);
		noteAdditions([{ addedNodes: [added] } as unknown as MutationRecord]);
		height = 200;
		notify();
		expect(addedAnimate).not.toHaveBeenCalled();
		expect(revealAnimate).not.toHaveBeenCalled();
	});
});
