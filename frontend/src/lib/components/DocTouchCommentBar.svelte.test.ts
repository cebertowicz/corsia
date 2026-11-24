import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import DocTouchCommentBar from './DocTouchCommentBar.svelte';
let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;
afterEach(async () => { if (component) await unmount(component); component = undefined; host?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
it.each([320, 393, 768])('follows the visible viewport above keyboard/accessory bounds at %spx', width => {
	host = document.body.appendChild(document.createElement('div'));
	const viewport = Object.assign(new EventTarget(), { offsetTop: 0, height: 851 });
	vi.stubGlobal('innerHeight', 851);
	vi.stubGlobal('visualViewport', viewport);
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.spyOn(host, 'getBoundingClientRect').mockReturnValue(new DOMRect(12, 80, width - 24, 750));
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		return this.classList.contains('touch-comment-bar') ? new DOMRect(12, 791, width - 24, 60) : new DOMRect();
	});
	const onComment = vi.fn();
	const props = $state({ column: host, offered: true, onComment });
	component = mount(DocTouchCommentBar, { target: host, props }); flushSync();
	const bar = host.querySelector<HTMLElement>('.touch-comment-bar')!;
	const button = bar.querySelector('button')!;
	expect(bar.style.bottom).toBe('0px');
	expect(bar.style.left).toBe('12px');
	expect(bar.style.width).toBe(`${width - 24}px`);
	// The column (80 to 830) ends 21 px above the page's foot: the bar's top 39 px covers it, not its whole 60.
	expect(host.style.getPropertyValue('--document-comment-room')).toBe('39px');
	viewport.height = 450; viewport.offsetTop = 30;
	viewport.dispatchEvent(new Event('resize')); flushSync();
	expect(bar.style.bottom).toBe('371px');
	viewport.offsetTop = 45; viewport.dispatchEvent(new Event('scroll')); flushSync();
	expect(bar.style.bottom).toBe('356px');
	button.click(); expect(onComment).toHaveBeenCalledOnce();
	props.offered = false; flushSync();
	expect(bar.querySelector('button')).toBeNull();
	expect(bar.getAttribute('aria-hidden')).toBe('true');
	expect(host.style.getPropertyValue('--document-comment-room')).toBe('39px');
});
it('reserves the bar\'s whole height when the column runs to the page foot, and nothing when it ends above the bar', () => {
	host = document.body.appendChild(document.createElement('div'));
	vi.stubGlobal('innerHeight', 851);
	vi.stubGlobal('visualViewport', Object.assign(new EventTarget(), { offsetTop: 0, height: 851 }));
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	let columnBottom = 851;
	vi.spyOn(host, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0, 80, 393, columnBottom - 80));
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		return this.classList.contains('touch-comment-bar') ? new DOMRect(0, 791, 393, 60) : new DOMRect();
	});
	const props = $state({ column: host, offered: true, onComment: vi.fn() });
	component = mount(DocTouchCommentBar, { target: host, props }); flushSync();
	expect(host.style.getPropertyValue('--document-comment-room')).toBe('60px');
	columnBottom = 700; window.dispatchEvent(new Event('resize')); flushSync();
	expect(host.style.getPropertyValue('--document-comment-room')).toBe('0px');
});
it('follows the column to where it settles when it moves without resizing, and when the offer arrives', () => {
	host = document.body.appendChild(document.createElement('div'));
	vi.stubGlobal('innerHeight', 851);
	vi.stubGlobal('visualViewport', Object.assign(new EventTarget(), { offsetTop: 0, height: 851 }));
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	let left = 73;
	vi.spyOn(host, 'getBoundingClientRect').mockImplementation(() => new DOMRect(left, 80, 369, 771));
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		return this.classList.contains('touch-comment-bar') ? new DOMRect(0, 791, 369, 60) : new DOMRect();
	});
	const props = $state({ column: host, offered: false, onComment: vi.fn() });
	component = mount(DocTouchCommentBar, { target: host, props }); flushSync();
	const bar = host.querySelector<HTMLElement>('.touch-comment-bar')!;
	expect(bar.style.left).toBe('73px');
	left = 12; host.dispatchEvent(new Event('animationend', { bubbles: true })); flushSync();
	expect(bar.style.left, 'a column that slid into place').toBe('12px');
	left = 30; props.offered = true; flushSync();
	expect(bar.style.left, 'the offer arriving').toBe('30px');
});
