import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import Modal from './Modal.svelte';

let component: ReturnType<typeof mount> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = '';
});

describe('Modal', () => {
	it('hands focus back to its opener without scrolling the page', async () => {
		const opener = document.createElement('button');
		document.body.appendChild(opener);
		opener.focus();
		component = mount(Modal, { target: document.body, props: { title: 'Add model', onClose: vi.fn(), children: (() => {}) as never } });
		flushSync();
		await tick();
		const back = vi.spyOn(opener, 'focus');
		unmount(component);
		component = null;
		expect(back).toHaveBeenCalledTimes(1);
		expect(back).toHaveBeenCalledWith({ preventScroll: true });
	});
});
