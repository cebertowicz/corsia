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

const call = (args: Record<string, unknown>, over: Record<string, unknown> = {}) => (
	{ id: 'c1', name: 'browser_open', args, ...over }
);
// The server marks a settled, visible open; the message reads the mark and decides nothing.
const opened = (hostName: string | null = 'github.com') =>
	call({ url: 'https://github.com', show_window: true }, { browser_chip: { host: hostName } });

function render(calls: unknown[], over: Record<string, unknown> = {}): HTMLElement {
	if (component) unmount(component);
	host?.remove();
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(AssistantMessage, {
		target: host,
		props: {
			msg: { id: 'a1', thread_id: 't1', content: '', created_at: '2026-09-25T08:00:00Z', metadata: { tool_calls: calls }, ...over },
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
	it('shows the chip on a message with no cards whose tool call carries one', () => {
		const chips = render([opened()]).querySelectorAll('button.tile.chip');
		expect(chips).toHaveLength(1);
		expect(chips[0].getAttribute('aria-label')).toBe('Reopen the shared browser');
		expect(chips[0].querySelector('.title')?.textContent).toBe('github.com');
	});

	it('shows no strip for a call the server gave no chip, however it looks, or for no tool calls', () => {
		expect(render([call({ url: 'https://github.com', show_window: true })]).querySelector('.cards')).toBeNull();
		expect(render([call({ url: 'https://github.com', show_window: true }, { status: 'running' })]).querySelector('.cards')).toBeNull();
		expect(render([]).querySelector('.cards')).toBeNull();
	});

	it('keeps the chip a Focus row carries after its tool calls are stripped', () => {
		expect(render([], { browserChip: { host: 'github.com' } }).querySelectorAll('button.tile.chip')).toHaveLength(1);
		expect(render([opened()], { browserChip: null }).querySelector('.cards')).toBeNull();
	});
});
