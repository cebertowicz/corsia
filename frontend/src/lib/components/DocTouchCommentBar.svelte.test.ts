import { afterEach, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';
import DocTouchCommentBar from './DocTouchCommentBar.svelte';
let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;
// What the document hands the holder: its own Comment button, which carries the click.
const button = (onComment: () => void) => createRawSnippet(() => ({ render: () => '<button class="sel-comment">Comment</button>', setup: (element) => { element.addEventListener('click', onComment); } }));
afterEach(async () => { if (component) await unmount(component); component = undefined; host?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
it.each([320, 393, 768])('follows the visible viewport above keyboard/accessory bounds at %spx', width => {
	host = document.body.appendChild(document.createElement('div'));
	const viewport = Object.assign(new EventTarget(), { offsetTop: 0, height: 851 });
	vi.stubGlobal('innerHeight', 851);
	vi.stubGlobal('visualViewport', viewport);
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.spyOn(host, 'getBoundingClientRect').mockReturnValue(new DOMRect(12, 80, width - 24, 750));
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		// Where a browser has the bar: its top follows the `bottom` the page has written so far, which is the value before a move until the next flush.
		return this.classList.contains('touch-comment-bar') ? new DOMRect(12, 851 - (parseFloat(this.style.bottom) || 0) - 52, width - 24, 52) : new DOMRect();
	});
	const onComment = vi.fn();
	const props = $state({ column: host, offered: true, children: button(onComment) });
	component = mount(DocTouchCommentBar, { target: host, props }); flushSync();
	const bar = host.querySelector<HTMLElement>('.touch-comment-bar')!;
	const comment = bar.querySelector('button')!;
	expect(bar.style.bottom).toBe('0px');
	expect(bar.style.left).toBe('12px');
	expect(bar.style.width).toBe(`${width - 24}px`);
	// The column (80 to 830) ends 21 px above the page's foot: the holder's top 31 px covers it, not its whole 52.
	expect(host.style.getPropertyValue('--document-comment-room')).toBe('31px');
	viewport.height = 450; viewport.offsetTop = 30;
	viewport.dispatchEvent(new Event('resize')); flushSync();
	expect(bar.style.bottom).toBe('371px');
	// The keyboard is open: the button sits above it, wholly over the column, so it covers its whole 52 px, not the 31 px it covered at the page's foot.
	expect(host.style.getPropertyValue('--document-comment-room'), 'the room after the bar moved up').toBe('52px');
	viewport.offsetTop = 45; viewport.dispatchEvent(new Event('scroll')); flushSync();
	expect(bar.style.bottom).toBe('356px');
	expect(host.style.getPropertyValue('--document-comment-room')).toBe('52px');
	comment.click(); expect(onComment).toHaveBeenCalledOnce();
	// The keyboard closes: the button is back at the page's foot and covers 31 px again, not the 52 it covered above the keyboard.
	viewport.height = 851; viewport.offsetTop = 0;
	viewport.dispatchEvent(new Event('resize')); flushSync();
	expect(bar.style.bottom).toBe('0px');
	expect(host.style.getPropertyValue('--document-comment-room'), 'the room after the bar moved down').toBe('31px');
	props.offered = false; flushSync();
	expect(bar.querySelector('button')).toBeNull();
	expect(bar.getAttribute('aria-hidden')).toBe('true');
	expect(host.style.getPropertyValue('--document-comment-room')).toBe('31px');
});
it('reserves the holder\'s whole height when the column runs to the page foot, and nothing when it ends above the bar', () => {
	host = document.body.appendChild(document.createElement('div'));
	vi.stubGlobal('innerHeight', 851);
	vi.stubGlobal('visualViewport', Object.assign(new EventTarget(), { offsetTop: 0, height: 851 }));
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	let columnBottom = 851;
	vi.spyOn(host, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0, 80, 393, columnBottom - 80));
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		return this.classList.contains('touch-comment-bar') ? new DOMRect(0, 799, 393, 52) : new DOMRect();
	});
	const props = $state({ column: host, offered: true, children: button(vi.fn()) });
	component = mount(DocTouchCommentBar, { target: host, props }); flushSync();
	expect(host.style.getPropertyValue('--document-comment-room')).toBe('52px');
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
		return this.classList.contains('touch-comment-bar') ? new DOMRect(0, 799, 369, 52) : new DOMRect();
	});
	const props = $state({ column: host, offered: false, children: button(vi.fn()) });
	component = mount(DocTouchCommentBar, { target: host, props }); flushSync();
	const bar = host.querySelector<HTMLElement>('.touch-comment-bar')!;
	expect(bar.style.left).toBe('73px');
	left = 12; host.dispatchEvent(new Event('animationend', { bubbles: true })); flushSync();
	expect(bar.style.left, 'a column that slid into place').toBe('12px');
	left = 30; props.offered = true; flushSync();
	expect(bar.style.left, 'the offer arriving').toBe('30px');
});
