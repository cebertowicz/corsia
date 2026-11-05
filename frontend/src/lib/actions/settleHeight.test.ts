import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { carryPanelHeight, settleHeight } from './settleHeight';

let height = 300;
let notify: () => void = () => {};
const animate = vi.fn();
let finish: () => void = () => {};
let cancel: () => void = () => {};
const observe = vi.fn();
const unobserve = vi.fn();

beforeEach(() => {
	height = 300;
	observe.mockReset();
	unobserve.mockReset();
	animate.mockReset();
	animate.mockImplementation(() => ({
		addEventListener: (type: string, listener: () => void) => {
			if (type === 'finish') finish = listener;
			if (type === 'cancel') cancel = listener;
		},
		cancel: () => cancel(),
	}));
	vi.stubGlobal('ResizeObserver', class {
		constructor(callback: () => void) { notify = callback; }
		observe = observe;
		unobserve = unobserve;
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
	it('pauses observation during the panel animation and resumes after it finishes', () => {
		const node = panel();
		settleHeight(node, { enabled: true });
		height = 200;
		notify();
		expect(unobserve).toHaveBeenCalledWith(node);
		expect(unobserve.mock.invocationCallOrder[0]).toBeLessThan(animate.mock.invocationCallOrder[0]);
		expect(observe).toHaveBeenCalledTimes(1);
		finish();
		expect(observe).toHaveBeenCalledTimes(2);
		expect(observe).toHaveBeenLastCalledWith(node);
	});

	it('resumes observation when an animation is cancelled', () => {
		const node = panel();
		settleHeight(node, { enabled: true });
		height = 200;
		notify();
		cancel();
		expect(observe).toHaveBeenCalledTimes(2);
		expect(observe).toHaveBeenLastCalledWith(node);
	});

	it('does not resume observation when a running animation is destroyed', () => {
		const node = panel();
		const action = settleHeight(node, { enabled: true });
		height = 200;
		notify();
		action.destroy();
		expect(observe).toHaveBeenCalledTimes(1);
	});

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

	it('eases a later size change after the first animation finishes', () => {
		const node = panel();
		settleHeight(node, { enabled: true });
		height = 200;
		notify();
		finish();
		height = 120;
		notify();
		expect(animate).toHaveBeenCalledTimes(2);
		expect((animate.mock.calls[1][0] as Array<{ height: string }>).map((frame) => frame.height)).toEqual(['200px', '120px']);
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
		expect(observe).toHaveBeenCalledTimes(1);
		expect((animate.mock.calls[1][0] as Array<{ height: string }>).map((frame) => frame.height)).toEqual(['200px', '120px']);
		finish();
		expect(observe).toHaveBeenCalledTimes(2);
	});

	it('shows no frame at the content\'s own height between the end of an ease and its handling, then eases on from the end height', () => {
		// A small model of the animations: a frame draws the newest animation that still applies, else the content's own height.
		type Fake = { frames: Array<{ height: string }>; fill: string; ended: boolean; cancelled: boolean; finish: () => void };
		const fakes: Fake[] = [];
		animate.mockImplementation((frames: Array<{ height: string }>, options: { fill: string }) => {
			const fake: Fake = { frames, fill: options.fill, ended: false, cancelled: false, finish: () => {} };
			fakes.push(fake);
			return {
				addEventListener: (type: string, listener: () => void) => { if (type === 'finish') fake.finish = listener; },
				cancel: () => { fake.cancelled = true; },
			};
		});
		const drawn = () => {
			for (const fake of [...fakes].reverse()) {
				if (fake.cancelled) continue;
				if (!fake.ended) return parseFloat(fake.frames[0].height);
				if (fake.fill === 'both' || fake.fill === 'forwards') return parseFloat(fake.frames[1].height);
			}
			return height;
		};
		const node = panel();
		node.getBoundingClientRect = () => ({ height: drawn() }) as DOMRect;
		settleHeight(node, { enabled: true });
		height = 200;
		notify();
		// While it runs the content shrinks on to 120 behind it, and the ease reaches its end.
		height = 120;
		fakes[0].ended = true;
		// The frame after the end is drawn before the finish is handled.
		expect(drawn()).toBe(200);
		fakes[0].finish();
		// The ease on starts from the end height and takes over from the held one in the same task.
		expect(fakes).toHaveLength(2);
		expect(fakes[0].cancelled).toBe(true);
		expect(fakes[1].frames.map((frame) => frame.height)).toEqual(['200px', '120px']);
		expect(drawn()).toBe(200);
	});

	it('leaves content that eases its own height to carry the panel, frame by frame', () => {
		const node = panel();
		node.getAnimations = (() => [{ playState: 'running', effect: { getKeyframes: () => [{ height: '0px', opacity: 0 }] } }]) as unknown as typeof node.getAnimations;
		settleHeight(node, { enabled: true });
		height = 200;
		notify();
		expect(animate).not.toHaveBeenCalled();
	});

	it('leaves a change that content easing ended before the panel looked, and eases the next one', async () => {
		const frames: FrameRequestCallback[] = [];
		vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
		const node = panel();
		const content = document.createElement('div');
		// The carried content is the whole of the panel's height here, so every panel change is its own.
		content.getBoundingClientRect = () => ({ height: height - 100 }) as DOMRect;
		node.append(content);
		settleHeight(node, { enabled: true });
		let end: () => void = () => {};
		carryPanelHeight(content, new Promise<void>((resolve) => { end = resolve; }));
		// The ease ran its whole course between two looks: the panel sees only the end height, with nothing running.
		height = 120;
		notify();
		expect(animate).not.toHaveBeenCalled();
		end();
		await Promise.resolve(); await Promise.resolve();
		// Held through the frame that laid out the end height, then released one frame later.
		frames.shift()?.(0);
		height = 90;
		notify();
		expect(animate).not.toHaveBeenCalled();
		frames.shift()?.(0);
		height = 40;
		notify();
		expect(animate).toHaveBeenCalledTimes(1);
		vi.unstubAllGlobals();
	});

	describe('a resize during a hold that the carried content did not make', () => {
		/** A panel of 300 holding carried content of 100, the content's own ease not yet ended. */
		const held = () => {
			const node = panel();
			const content = document.createElement('div');
			let own = 100;
			content.getBoundingClientRect = () => ({ height: own }) as DOMRect;
			node.append(content);
			settleHeight(node, { enabled: true });
			carryPanelHeight(content, new Promise<void>(() => {}));
			// First look of the held content: all of that look is its own.
			height = 310;
			own = 110;
			notify();
			return { node, resize: (panelHeight: number, contentHeight: number) => { height = panelHeight; own = contentHeight; notify(); } };
		};

		it('still eases, from where that part began', () => {
			const { resize } = held();
			// The content grows 10 on its own while something else in the panel loses 24.
			resize(296, 120);
			expect(animate).toHaveBeenCalledTimes(1);
			expect((animate.mock.calls[0][0] as Array<{ height: string }>).map((frame) => frame.height)).toEqual(['320px', '296px']);
		});

		it('leaves a change the content made itself to the content', () => {
			const { resize } = held();
			resize(330, 130);
			resize(350, 150);
			expect(animate).not.toHaveBeenCalled();
		});

		it('leaves the content\'s change alone when the other part moves by a pixel or less', () => {
			const { resize } = held();
			resize(320.5, 120);
			expect(animate).not.toHaveBeenCalled();
		});

		it('under reduced motion cuts it and fades the new content in', () => {
			const node = panel();
			const content = document.createElement('div');
			const body = document.createElement('section');
			const bodyAnimate = vi.fn();
			body.animate = bodyAnimate as unknown as typeof body.animate;
			content.getBoundingClientRect = () => ({ height: 100 }) as DOMRect;
			node.append(body, content);
			settleHeight(node, { enabled: true, reduced: true });
			carryPanelHeight(content, new Promise<void>(() => {}));
			height = 300.5;
			notify();
			height = 270;
			notify();
			expect(animate).not.toHaveBeenCalled();
			expect(bodyAnimate).toHaveBeenCalledTimes(1);
		});
	});

	it('still eases while only a fade runs inside the panel', () => {
		const node = panel();
		node.getAnimations = (() => [{ playState: 'running', effect: { getKeyframes: () => [{ opacity: 0 }, { opacity: 1 }] } }]) as unknown as typeof node.getAnimations;
		settleHeight(node, { enabled: true });
		height = 200;
		notify();
		expect(animate).toHaveBeenCalledTimes(1);
	});

	it('under reduced motion cuts the size and fades the new content in instead', () => {
		const node = panel();
		const chrome = document.createElement('div');
		chrome.className = 'drawer-chrome';
		const body = document.createElement('section');
		const bodyAnimate = vi.fn();
		const chromeAnimate = vi.fn();
		body.animate = bodyAnimate as unknown as typeof body.animate;
		chrome.animate = chromeAnimate as unknown as typeof chrome.animate;
		node.append(chrome, body);
		settleHeight(node, { enabled: true, reduced: true });
		height = 200;
		notify();
		expect(animate).not.toHaveBeenCalled();
		expect(chromeAnimate).not.toHaveBeenCalled();
		expect(bodyAnimate).toHaveBeenCalledTimes(1);
		expect((bodyAnimate.mock.calls[0][0] as Array<{ opacity: number }>).map((frame) => frame.opacity)).toEqual([0, 1]);
	});
});
