import { flushSync, mount, tick, unmount } from 'svelte';
import { expect, it, vi } from 'vitest';
import ReplyField from './ReplyField.svelte';

it('reveals the input when an unfocused reply receives a programmatic input event', async () => {
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { queueMicrotask(() => callback(0)); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	const owner = document.createElement('div');
	owner.style.overflowY = 'auto';
	Object.defineProperty(owner, 'clientHeight', { value: 100 });
	const card = document.createElement('div');
	card.getBoundingClientRect = () => ({ top: 0, bottom: 300, height: 300 }) as DOMRect;
	card.scrollIntoView = vi.fn();
	owner.append(card);
	document.body.append(owner);
	const component = mount(ReplyField, { target: card, props: { keepVisible: true, unfocused: 'card-top', wholeCard: card, onSend: vi.fn() } });
	try {
		flushSync();
		await tick();
		const field = card.querySelector('textarea')!;
		field.scrollIntoView = vi.fn();
		expect(document.activeElement).not.toBe(field);
		field.value = 'restored reply';
		field.setSelectionRange(field.value.length, field.value.length);
		field.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		await tick();
		expect(field.scrollIntoView).toHaveBeenCalled();
	} finally {
		unmount(component);
		owner.remove();
		vi.unstubAllGlobals();
	}
});
