import { afterEach, expect, it, vi } from 'vitest';
import { createReplyVisibility } from './replyVisibility';

it('detects a tall reply filling and leaving a shorter scroller between observer thresholds and removes its listener', () => {
	let callback: IntersectionObserverCallback = () => {};
	class Observer {
		constructor(cb: IntersectionObserverCallback) { callback = cb; }
		observe() {}
		disconnect() {}
	}
	vi.stubGlobal('IntersectionObserver', Observer);
	const root = document.createElement('div');
	const reply = document.createElement('div');
	reply.dataset.replyId = 'r1';
	root.append(reply);
	document.body.append(root);
	vi.spyOn(window, 'getComputedStyle').mockImplementation((element) => ({ overflowY: element === root ? 'auto' : 'visible' }) as CSSStyleDeclaration);
	vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({ top: 100, bottom: 400, height: 300 } as DOMRect);
	let top = 110;
	vi.spyOn(reply, 'getBoundingClientRect').mockImplementation(() => ({ top, bottom: top + 1000, height: 1000 }) as DOMRect);
	const add = vi.spyOn(document, 'addEventListener');
	const remove = vi.spyOn(document, 'removeEventListener');
	const onVisible = vi.fn();
	const onHidden = vi.fn();
	const visibility = createReplyVisibility(onVisible, onHidden, vi.fn());
	visibility.sync(root, true, ['r1']);
	const entry = { isIntersecting: true, intersectionRatio: 0.29, intersectionRect: { height: 290 }, boundingClientRect: { height: 1000 }, rootBounds: { top: 0, height: 800 } } as IntersectionObserverEntry;
	callback([entry], {} as IntersectionObserver);
	expect(onHidden).toHaveBeenCalledTimes(1);
	expect(onVisible).not.toHaveBeenCalled();
	expect(add).toHaveBeenCalledWith('scroll', expect.any(Function), { capture: true, passive: true });
	top = 90;
	root.dispatchEvent(new Event('scroll'));
	expect(onVisible).toHaveBeenCalledTimes(1);
	root.dispatchEvent(new Event('scroll'));
	expect(onVisible).toHaveBeenCalledTimes(1);
	top = 110;
	root.dispatchEvent(new Event('scroll'));
	expect(onHidden).toHaveBeenCalledTimes(2);
	callback([{ isIntersecting: false } as IntersectionObserverEntry], {} as IntersectionObserver);
	expect(remove).toHaveBeenCalledTimes(1);
	callback([entry], {} as IntersectionObserver);
	visibility.sync(root, true, []);
	expect(remove).toHaveBeenCalledTimes(2);
	visibility.sync(root, true, ['r1']);
	callback([entry], {} as IntersectionObserver);
	visibility.clear();
	expect(remove).toHaveBeenCalledTimes(3);
	expect(remove).toHaveBeenCalledWith('scroll', expect.any(Function), true);
	top = 90;
	root.dispatchEvent(new Event('scroll'));
	expect(onVisible).toHaveBeenCalledTimes(1);
	root.remove();
});

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
