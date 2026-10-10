import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { openSubagentRequest } from '$lib/stores/subagents';
import AgentMessageAttribution from './AgentMessageAttribution.svelte';

function agentMsg(overrides: Record<string, unknown> = {}) {
	return {
		id: 'm-agent-1',
		role: 'user',
		content: 'run the tests',
		metadata: {
			sender_kind: 'agent',
			from_thread: 'thread-conductor',
			from_title: 'Conductor',
			...overrides,
		},
	};
}

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	openSubagentRequest.set(null);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

function render(msg: unknown): HTMLElement {
	component = mount(AgentMessageAttribution, { target: host, props: { msg } });
	flushSync();
	const chip = host.querySelector<HTMLElement>('button.agent-attr');
	expect(chip).not.toBeNull();
	return chip!;
}

describe('AgentMessageAttribution', () => {
	it('renders the sending thread title', () => {
		const chip = render(agentMsg());
		expect(chip.textContent).toContain('Conductor');
	});

	it('shows the ▸ glyph', () => {
		const chip = render(agentMsg());
		expect(chip.querySelector('.attr-glyph')?.textContent).toBe('▸');
	});

	it('click requests opening the sending thread', () => {
		const chip = render(agentMsg());
		chip.click();
		flushSync();
		expect(get(openSubagentRequest)).toBe('thread-conductor');
	});

	it('click with no from_thread is a no-op (degenerate metadata)', () => {
		const chip = render(agentMsg({ from_thread: '' }));
		chip.click();
		flushSync();
		expect(get(openSubagentRequest)).toBeNull();
	});

	it('falls back to "agent" label when from_title is empty', () => {
		const chip = render(agentMsg({ from_title: '' }));
		expect(chip.querySelector('.attr-title')?.textContent).toBe('agent');
	});
});
