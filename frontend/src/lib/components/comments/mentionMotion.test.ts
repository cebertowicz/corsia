import { afterEach, describe, expect, it, vi } from 'vitest';
import { playMentionLayout } from '$lib/actions/layoutCrossfade';

function fakeAnimation() {
	return { cancel: vi.fn(), finished: new Promise<void>(() => {}), playState: 'running' };
}

describe('playMentionLayout', () => {
	afterEach(() => { vi.restoreAllMocks(); document.body.innerHTML = ''; });

	it('hands a fade that is still running to the next one, with the same copy and the opacities reached', () => {
		const overlay = document.createElement('div');
		const live = document.createElement('div');
		document.body.append(live);
		overlay.style.opacity = '0.6';
		live.style.opacity = '0.4';
		const animate = vi.fn(() => fakeAnimation());
		overlay.animate = animate as unknown as typeof overlay.animate;
		live.animate = animate as unknown as typeof live.animate;
		const playing = playMentionLayout({ overlay, height: 54, scroll: [] }, live, true);
		const handed = playing.handoff();
		expect(handed?.overlay).toBe(overlay);
		expect(handed?.from).toBe(0.6);
		expect(handed?.liveFrom).toBe(0.4);
		expect(animate.mock.results.every((result) => (result.value as ReturnType<typeof fakeAnimation>).cancel.mock.calls.length === 1)).toBe(true);
		expect(document.body.contains(overlay)).toBe(true);
	});

	it('stops a fade it will not hand on, and reports the opacities that were on screen', () => {
		const overlay = document.createElement('div');
		const live = document.createElement('div');
		document.body.append(overlay, live);
		overlay.style.opacity = '0.6';
		live.style.opacity = '0.4';
		const animate = vi.fn(() => fakeAnimation());
		overlay.animate = animate as unknown as typeof overlay.animate;
		live.animate = animate as unknown as typeof live.animate;
		const playing = playMentionLayout({ overlay, height: 54, scroll: [] }, live, true, false);
		const seen = playing.supersede();
		expect(seen).toEqual({ from: 0.6, liveFrom: 0.4, midFade: true });
		expect(document.body.contains(overlay)).toBe(false);
		expect(animate.mock.results.every((result) => (result.value as ReturnType<typeof fakeAnimation>).cancel.mock.calls.length === 1)).toBe(true);
	});

	it('reports no mid-fade once its own fade is over, so the next change crossfades from the settled layout', async () => {
		const overlay = document.createElement('div');
		const live = document.createElement('div');
		document.body.append(overlay, live);
		overlay.animate = (() => ({ cancel: vi.fn(), finished: Promise.resolve(), playState: 'finished' })) as unknown as typeof overlay.animate;
		live.animate = (() => fakeAnimation()) as unknown as typeof live.animate;
		const playing = playMentionLayout({ overlay, height: 54, scroll: [] }, live, true);
		await new Promise((resolve) => setTimeout(resolve));
		expect(playing.supersede()).toEqual({ from: 1, liveFrom: 0, midFade: false });
	});

	it('hands nothing on once its fade has finished, so the next change captures the layout as it is', async () => {
		const overlay = document.createElement('div');
		const live = document.createElement('div');
		document.body.append(live);
		const finished = Promise.resolve();
		overlay.animate = (() => ({ cancel: vi.fn(), finished, playState: 'finished' })) as unknown as typeof overlay.animate;
		live.animate = (() => fakeAnimation()) as unknown as typeof live.animate;
		const playing = playMentionLayout({ overlay, height: 54, scroll: [] }, live, true);
		await new Promise((resolve) => setTimeout(resolve));
		expect(playing.handoff()).toBeNull();
		expect(document.body.contains(overlay)).toBe(false);
	});

	it('starts the next fade from the opacities it was handed', () => {
		const overlay = document.createElement('div');
		const live = document.createElement('div');
		document.body.append(live);
		const overlayAnimate = vi.fn(() => fakeAnimation());
		const liveAnimate = vi.fn(() => fakeAnimation());
		overlay.animate = overlayAnimate as unknown as typeof overlay.animate;
		live.animate = liveAnimate as unknown as typeof live.animate;
		playMentionLayout({ overlay, height: 54, scroll: [], from: 0.6, liveFrom: 0.4 }, live, true);
		expect(overlayAnimate).toHaveBeenCalledWith([{ opacity: 0.6 }, { opacity: 0 }], expect.anything());
		expect(liveAnimate).toHaveBeenCalledWith([{ opacity: 0.4 }, { opacity: 1 }], expect.anything());
	});

	it('eases from what is visible to the content height while a transition still holds the region at its own', () => {
		const overlay = document.createElement('div');
		const live = document.createElement('div');
		const child = document.createElement('div');
		live.append(child);
		document.body.append(live);
		live.getBoundingClientRect = () => ({ top: 100, height: 30 }) as DOMRect;
		child.getBoundingClientRect = () => ({ bottom: 284 }) as DOMRect;
		const finish = vi.fn();
		live.getAnimations = () => [{ effect: { getKeyframes: () => [{ height: '45.5px' }] }, finish }] as unknown as Animation[];
		const overlayAnimate = vi.fn(() => fakeAnimation());
		const liveAnimate = vi.fn(() => fakeAnimation());
		overlay.animate = overlayAnimate as unknown as typeof overlay.animate;
		live.animate = liveAnimate as unknown as typeof live.animate;
		const playing = playMentionLayout({ overlay, height: 30, scroll: [] }, live, false);
		expect(liveAnimate).toHaveBeenCalledWith([{ height: '30px', overflow: 'hidden' }, { height: '184px', overflow: 'hidden' }], expect.anything());
		expect(finish).toHaveBeenCalledTimes(1);
		playing.cancel();
	});

	it('reads the region itself when nothing holds its height', () => {
		const overlay = document.createElement('div');
		const live = document.createElement('div');
		document.body.append(live);
		live.getBoundingClientRect = () => ({ top: 100, height: 184 }) as DOMRect;
		live.getAnimations = () => [];
		const liveAnimate = vi.fn(() => fakeAnimation());
		overlay.animate = (() => fakeAnimation()) as unknown as typeof overlay.animate;
		live.animate = liveAnimate as unknown as typeof live.animate;
		const playing = playMentionLayout({ overlay, height: 54, scroll: [] }, live, false);
		expect(liveAnimate).toHaveBeenCalledWith([{ height: '54px', overflow: 'hidden' }, { height: '184px', overflow: 'hidden' }], expect.anything());
		playing.cancel();
	});
});
