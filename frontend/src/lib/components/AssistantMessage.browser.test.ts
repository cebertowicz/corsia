import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';

import AssistantMessage from './AssistantMessage.svelte';

let host: HTMLElement | null = null;
let component: Record<string, unknown> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	host = null;
});

const open = { id: 'c1', name: 'browser_open', args: { url: 'https://github.com', show_window: true } };

function render(metadata: Record<string, unknown>): HTMLElement {
	if (component) unmount(component);
	host?.remove();
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(AssistantMessage, {
		target: host,
		props: {
			msg: { id: 'a1', thread_id: 't1', content: '', created_at: '2026-09-25T08:00:00Z', metadata },
			expandedTools: new Set<string>(),
			expandedReasoning: new Set<string>(),
			onToggleToolBlock: () => {},
			onToggleReasoning: () => {},
		},
	});
	flushSync();
	return host;
}

describe('AssistantMessage browser chip', () => {
	// The server stores the chip on the Step as a visible open succeeds; the message reads it and decides nothing.
	it('shows the chip the Step carries', () => {
		const chips = render({ tool_calls: [open], browser_chip: { host: 'github.com' } }).querySelectorAll('button.tile.chip');
		expect(chips).toHaveLength(1);
		expect(chips[0].getAttribute('title')).toBe('Reopen the shared browser');
		expect(chips[0].querySelector('.title')?.textContent).toBe('github.com');
	});

	it('names the shared browser when the open had no host', () => {
		expect(render({ browser_chip: { host: null } }).querySelector('button.tile.chip .title')?.textContent).toBe('Shared browser');
	});

	it('shows no strip for a Step with no stored chip, however its calls look', () => {
		expect(render({ tool_calls: [open] }).querySelector('.cards')).toBeNull();
		expect(render({ tool_calls: [{ ...open, browser_chip: { host: 'github.com' } }] }).querySelector('.cards')).toBeNull();
		expect(render({ browser_chip: { host: 3 } }).querySelector('.cards')).toBeNull();
		expect(render({}).querySelector('.cards')).toBeNull();
	});
});
