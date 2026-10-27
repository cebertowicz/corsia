import { flushSync, mount, unmount } from 'svelte';
import { createRawSnippet } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import NeedsYouTile from './NeedsYouTile.svelte';

let component: Record<string, unknown> | undefined;
let host: HTMLDivElement;
const lead = createRawSnippet(() => ({ render: () => '<span>!</span>' }));

function render(props: { twoLine?: boolean }) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(NeedsYouTile, { target: host, props: { title: 'A long title', lead, ...props } });
	flushSync();
}
afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
});

describe('NeedsYouTile title clamp', () => {
	it('is one line unless twoLine is asked for', () => {
		render({});
		expect(host.querySelector('.title')!.classList.contains('two-line')).toBe(false);
	});

	it('twoLine marks the title for the two-line clamp', () => {
		render({ twoLine: true });
		expect(host.querySelector('.title')!.classList.contains('two-line')).toBe(true);
	});
});
