import { readFileSync } from 'node:fs';
import { flushSync, mount, unmount } from 'svelte';
import { compile } from 'svelte/compiler';
import { afterEach, describe, expect, it } from 'vitest';
import SetupShell from './SetupShell.svelte';

let component: ReturnType<typeof mount> | null = null;
afterEach(async () => { if (component) await unmount(component); component = null; document.body.innerHTML = ''; });

function body() {
	component = mount(SetupShell, { target: document.body, props: { steps: [], page: 'welcome', title: 'Welcome' } });
	flushSync();
	return document.querySelector<HTMLElement>('.body')!;
}
/** jsdom lays nothing out, so a scroller's metrics are set by hand. */
function metrics(node: HTMLElement, m: { scrollTop: number; clientHeight: number; scrollHeight: number }) {
	for (const [key, value] of Object.entries(m)) Object.defineProperty(node, key, { configurable: true, value });
	node.dispatchEvent(new Event('scroll'));
}

describe('SetupShell scroll cue', () => {
	it('keeps the dock-edge fade in the compiled CSS, so the cue draws when content sits under the dock', () => {
		const source = readFileSync('src/lib/components/SetupShell.svelte', 'utf8');
		const { css } = compile(source, { filename: 'SetupShell.svelte', generate: 'client', css: 'external' });
		expect(css?.code).toMatch(/\[data-more='true'\][^{]*\{[^}]*mask-image:\s*linear-gradient/);
		expect(css?.code).toMatch(/\[data-above='true'\][^{]*\{[^}]*mask-image:\s*linear-gradient/);
	});

	it('cues only the edge that has more behind it', () => {
		const node = body();
		metrics(node, { scrollTop: 0, clientHeight: 300, scrollHeight: 400 });
		expect([node.dataset.above, node.dataset.more]).toEqual(['false', 'true']);
		metrics(node, { scrollTop: 50, clientHeight: 300, scrollHeight: 400 });
		expect([node.dataset.above, node.dataset.more]).toEqual(['true', 'true']);
		metrics(node, { scrollTop: 100, clientHeight: 300, scrollHeight: 400 });
		expect([node.dataset.above, node.dataset.more]).toEqual(['true', 'false']);
		metrics(node, { scrollTop: 0, clientHeight: 400, scrollHeight: 400 });
		expect([node.dataset.above, node.dataset.more]).toEqual(['false', 'false']);
	});
});
