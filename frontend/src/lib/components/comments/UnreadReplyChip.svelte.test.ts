import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import UnreadReplyChip from './UnreadReplyChip.svelte';
import { token } from '$lib/stores/auth';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function render(props: Partial<{ count: number; where: string | null; path: string | null; threadId: string | null; onOpen: () => void }> = {}) {
	const onOpen = props.onOpen ?? vi.fn();
	component = mount(UnreadReplyChip, { target: host, props: { artifactId: 'doc-1', path: '/tmp/launch-plan.md', threadId: 'thread-1', count: 2, onOpen, ...props } });
	flushSync();
	return onOpen;
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('test-bearer');
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
});

describe('UnreadReplyChip', () => {
	it('says how many replies are new, with the shared New marker', () => {
		render({ count: 2 });
		const chip = host.querySelector<HTMLButtonElement>('button.tile.chip');
		expect(chip?.textContent).toContain('2 new replies');
		expect(chip?.textContent).not.toContain('·');
		expect(chip?.querySelector('.ct-new-dot')).not.toBeNull();
		expect(chip?.getAttribute('aria-label')).toBe('2 new replies, document');
	});

	it('is singular for one reply and names where when the caller says so', () => {
		render({ count: 1, where: 'launch-plan.md' });
		expect(host.querySelector('button.tile.chip')?.textContent).toContain('1 new reply · launch-plan.md');
	});

	it('draws nothing when no reply is waiting', () => {
		render({ count: 0 });
		expect(host.querySelector('.reply-chip-notice')).toBeNull();
	});

	it('hands the tap to its caller and never opens a tab of its own', () => {
		const tabs = vi.fn();
		document.addEventListener('click', tabs);
		const onOpen = render();
		host.querySelector<HTMLButtonElement>('button.tile.chip')!.click();
		document.removeEventListener('click', tabs);
		expect(onOpen).toHaveBeenCalledOnce();
		expect(tabs).not.toHaveBeenCalled();
	});

	it('uses the supplied destination even when the document has no path', () => {
		const onOpen = render({ path: null });
		const chip = host.querySelector<HTMLButtonElement>('button.tile.chip');
		expect(chip).not.toBeNull();
		chip!.click();
		expect(onOpen).toHaveBeenCalledOnce();
	});

	it('stays a tappable button when the document has no thread of its own', () => {
		const onOpen = render({ threadId: null });
		const chip = host.querySelector<HTMLButtonElement>('button.tile.chip');
		expect(chip).not.toBeNull();
		chip!.click();
		expect(onOpen).toHaveBeenCalledOnce();
	});
});
