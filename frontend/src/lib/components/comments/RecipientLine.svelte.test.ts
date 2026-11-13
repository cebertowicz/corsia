import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import RecipientLine from './RecipientLine.svelte';

let host: HTMLElement;
let component: Record<string, unknown>;
afterEach(() => { if (component) unmount(component); host?.remove(); vi.restoreAllMocks(); });

function render(props: Record<string, unknown>) {
	host = document.createElement('div'); document.body.append(host);
	component = mount(RecipientLine, { target: host, props: props as never }); flushSync();
}
const button = () => host.querySelector('button') as HTMLButtonElement;

describe('RecipientLine', () => {
	it('names who gets it, with the comma on the name before it', () => {
		render({ names: ['Docs lane', 'Agent Substrate', 'Release lane'] });
		// Each name is one item with its own comma; "and" is a word of its own before the last name.
		expect(Array.from(host.querySelectorAll('.rl-item'), (item) => item.textContent?.trim())).toEqual(['Docs lane,', 'Agent Substrate', 'Release lane']);
		expect(Array.from(host.querySelectorAll('.rl-who > .rl-text'), (text) => text.textContent)).toEqual(['Goes to', 'and']);
	});

	it('says nobody gets it in its own sentence', () => {
		render({ names: [], none: 'No one gets this reply.' });
		expect(host.textContent).toContain('No one gets this reply.');
	});

	it('has no mute control: that lives with the card\'s other actions', () => {
		render({ names: ['Docs lane'] });
		expect(host.querySelector('button')).toBeNull();
	});

	it('never cuts a long name: it is written out in full and may break inside the word', () => {
		const long = 'Documents-and-Artifacts-vertical-owner-lane-with-a-very-long-name-9f3a';
		render({ names: [long] });
		expect(host.querySelector('.rl-name')?.textContent).toBe(long);
		expect(host.querySelector('.rl-name [title]')).toBeNull();
	});

	it('says it is checking, then offers Retry when the check failed', () => {
		render({ names: [], notice: 'loading', muted: false });
		expect(host.textContent).toContain('Checking who gets this…');
		expect(host.querySelector('button')).toBeNull();
		unmount(component); host.remove();
		const onRetry = vi.fn();
		render({ names: [], notice: 'error', onRetry });
		expect(host.textContent).toContain('Couldn’t check who gets this.');
		button().click();
		expect(onRetry).toHaveBeenCalledTimes(1);
	});
});
