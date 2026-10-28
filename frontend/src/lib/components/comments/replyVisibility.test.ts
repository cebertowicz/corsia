import { afterEach, expect, it, vi } from 'vitest';
import { createReplyVisibility } from './replyVisibility';

function entry(intersectionRatio: number, visible: number, height = 1000) {
	return { isIntersecting: intersectionRatio > 0, intersectionRatio, intersectionRect: { height: visible },
		boundingClientRect: { height }, rootBounds: { top: 0, height: 800 } } as IntersectionObserverEntry;
}

it('counts a tall reply once its visible part fills a shorter scroller, within one observer step', () => {
	let callback: IntersectionObserverCallback = () => {};
	const disconnect = vi.fn();
	class Observer {
		constructor(cb: IntersectionObserverCallback) { callback = cb; }
		observe() {}
		disconnect = disconnect;
	}
	vi.stubGlobal('IntersectionObserver', Observer);
	const root = document.createElement('div');
	const reply = document.createElement('div');
	reply.dataset.replyId = 'r1';
	root.append(reply);
	document.body.append(root);
	vi.spyOn(window, 'getComputedStyle').mockImplementation((element) => ({ overflowY: element === root ? 'hidden' : 'visible' }) as CSSStyleDeclaration);
	vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({ top: 100, bottom: 433, height: 333 } as DOMRect);
	const onVisible = vi.fn();
	const onHidden = vi.fn();
	const visibility = createReplyVisibility(onVisible, onHidden, vi.fn());
	visibility.sync(root, true, ['r1']);
	// The 1000px reply fills the 333px clip at ratio 0.333, so its last callback is the 0.30 threshold.
	callback([entry(0.25, 250)], {} as IntersectionObserver);
	expect(onHidden).toHaveBeenCalledTimes(1);
	callback([entry(0.3, 300)], {} as IntersectionObserver);
	expect(onVisible).toHaveBeenCalledTimes(1);
	callback([entry(0.25, 250)], {} as IntersectionObserver);
	expect(onHidden).toHaveBeenCalledTimes(2);
	callback([{ isIntersecting: false } as IntersectionObserverEntry], {} as IntersectionObserver);
	expect(onHidden).toHaveBeenCalledTimes(2);
	visibility.clear();
	expect(disconnect).toHaveBeenCalledTimes(1);
	root.remove();
});

it('counts a short reply only when fully visible', () => {
	let callback: IntersectionObserverCallback = () => {};
	vi.stubGlobal('IntersectionObserver', class { constructor(cb: IntersectionObserverCallback) { callback = cb; } observe() {} disconnect() {} });
	const root = document.createElement('div');
	const reply = document.createElement('div');
	reply.dataset.replyId = 'r1';
	root.append(reply);
	document.body.append(root);
	const onVisible = vi.fn();
	const visibility = createReplyVisibility(onVisible, vi.fn(), vi.fn());
	visibility.sync(root, true, ['r1']);
	callback([entry(0.95, 95, 100)], {} as IntersectionObserver);
	expect(onVisible).not.toHaveBeenCalled();
	callback([entry(1, 100, 100)], {} as IntersectionObserver);
	expect(onVisible).toHaveBeenCalledTimes(1);
	root.remove();
});

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
