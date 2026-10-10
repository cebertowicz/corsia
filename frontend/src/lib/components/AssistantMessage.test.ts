import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import AssistantMessage from './AssistantMessage.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
});

function render(metadata: Record<string, unknown> | null): HTMLElement {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(AssistantMessage, {
		target: host,
		props: {
			msg: {
				id: 'assistant-1',
				content: 'A durable update',
				created_at: '2026-09-25T08:00:00Z',
				metadata,
			},
			expandedTools: new Set<string>(),
			expandedReasoning: new Set<string>(),
			onToggleToolBlock: () => {},
			onToggleReasoning: () => {},
		},
	});
	flushSync();
	return host;
}

describe('AssistantMessage user-directed marker', () => {
	it('renders the visible Update label and dot for marked updates', () => {
		const rendered = render({ for_user: true, node_id: 'node-1' });
		const marker = rendered.querySelector<HTMLElement>('.for-you-marker');
		expect(marker?.textContent).toBe('Update');
		expect(marker?.getAttribute('title')).toBe('Update for you');
		expect(marker?.querySelector('.for-you-dot')).not.toBeNull();
	});

	it('renders the actual Codex model badge for a marked update with model metadata', () => {
		const rendered = render({
			for_user: true,
			model: 'codex/gpt-5-codex',
		});
		const badge = rendered.querySelector<HTMLElement>('.msg-model-badge');
		expect(badge?.textContent?.trim()).toBe('Codex · GPT-5 Codex');
	});

	it('does not render a marker for ordinary assistant messages', () => {
		const rendered = render({});
		expect(rendered.querySelector('.for-you-marker')).toBeNull();
	});
});

describe('AssistantMessage model name', () => {
	const badge = (metadata: Record<string, unknown>) => render(metadata).querySelector('.msg-model-badge')?.textContent?.replace(/\s+/g, ' ').trim();

	it('formats a Claude Code id with its version', () => {
		expect(badge({ harness: 'claude-code', model: 'claude-code/claude-opus-5-5' })).toBe('Claude Code · Claude Opus 5.5');
	});

	it('shows a stored display name as written', () => {
		expect(badge({ harness: 'claude-code', model: 'claude-haiku-4-5-20251001', model_name: 'Claude Haiku 4.5' }))
			.toBe('Claude Code · Claude Haiku 4.5');
	});
});
