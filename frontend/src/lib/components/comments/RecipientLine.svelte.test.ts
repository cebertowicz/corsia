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
	it('names who gets it as one run of text, with the comma on the name before it', () => {
		render({ names: ['Docs lane', 'Agent Substrate', 'Release lane'] });
		expect(host.querySelector('.rl-who')?.textContent?.replace(/\s+/g, ' ').trim()).toBe('Goes to Docs lane, Agent Substrate and Release lane');
		expect(Array.from(host.querySelectorAll('.rl-name'), (name) => name.textContent)).toEqual(['Docs lane', 'Agent Substrate', 'Release lane']);
	});

	it('says nobody gets it in its own sentence', () => {
		render({ names: [], none: 'No agent gets this reply.' });
		expect(host.textContent).toContain('No agent gets this reply.');
	});

	it('has no control of its own while it names someone', () => {
		render({ names: ['Docs lane'] });
		expect(host.querySelector('button')).toBeNull();
	});

	it('never cuts a long name: it is written out in full and may break inside the word', () => {
		const long = 'Documents-and-Artifacts-vertical-owner-lane-with-a-very-long-name-9f3a';
		render({ names: [long] });
		expect(host.querySelector('.rl-name')?.textContent).toBe(long);
		expect(host.querySelector('.rl-name [title]')).toBeNull();
	});

	it('carries the full list as its hover title so a cut row can still be read', () => {
		render({ names: ['Docs lane', 'Agent Substrate'] });
		expect(host.querySelector('.rl-who')?.getAttribute('title')).toBe('Docs lane, Agent Substrate');
		unmount(component); host.remove();
		render({ names: [], none: 'No agent gets this reply.' });
		expect(host.querySelector('.rl-who')?.hasAttribute('title')).toBe(false);
	});

	it('says it is checking, then offers Retry when the check failed', () => {
		render({ names: [], notice: 'loading' });
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
