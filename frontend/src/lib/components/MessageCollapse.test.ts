import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { activeThreadId } from '$lib/stores/threads';
import AssistantMessage from './AssistantMessage.svelte';
import UserMessageBubble from './UserMessageBubble.svelte';
import EventChip from './EventChip.svelte';

const LONG_TEXT = Array.from(
	{ length: 26 },
	(_, i) => `Line ${i + 1}: ${'readability '.repeat(8)}`
).join('\n');

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set('collapse-session');
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
});

function mountAssistant(content = LONG_TEXT) {
	component = mount(AssistantMessage, {
		target: host,
		props: {
			msg: {
				id: 'assistant-long',
				role: 'assistant',
				content,
				created_at: '2026-06-18T00:00:00',
				metadata: {},
			},
			expandedTools: new Set<string>(),
			expandedReasoning: new Set<string>(),
			onToggleToolBlock: () => {},
			onToggleReasoning: () => {},
		},
	});
	flushSync();
}

function mountUser(metadata: Record<string, unknown> = {}) {
	component = mount(UserMessageBubble, {
		target: host,
		props: {
			msg: {
				id: 'user-long',
				thread_id: 'collapse-session',
				role: 'user',
				content: LONG_TEXT,
				created_at: '2026-06-18T00:00:00',
				metadata,
			},
			onOpenLightbox: () => {},
		},
	});
	flushSync();
}

function mountEvent() {
	component = mount(EventChip, {
		target: host,
		props: {
			msg: {
				id: 'event-long',
				role: 'system',
				content: LONG_TEXT,
				metadata: {
					type: 'context_event',
					event_type: 'webhook.github.pr_comment',
					summary: LONG_TEXT,
					url: 'https://github.com/o/r/pull/42#issuecomment-1',
				},
			},
		},
	});
	flushSync();
}

function expectCollapsedThenExpandable(bodySelector: string) {
	const body = host.querySelector<HTMLElement>(bodySelector);
	const toggle = host.querySelector<HTMLButtonElement>('button.long-message-toggle');
	expect(body).not.toBeNull();
	expect(toggle).not.toBeNull();
	expect(body?.classList.contains('collapsed')).toBe(true);
	expect(toggle?.textContent).toBe('Show more');
	expect(toggle?.getAttribute('aria-expanded')).toBe('false');

	toggle?.click();
	flushSync();
	expect(body?.classList.contains('collapsed')).toBe(false);
	expect(toggle?.textContent).toBe('Show less');
	expect(toggle?.getAttribute('aria-expanded')).toBe('true');
}

describe('long message collapse', () => {
	it('does not auto-collapse long assistant final chat rows in the B5e source-record slice', () => {
		mountAssistant();
		expect(host.querySelector('button.long-message-toggle')).toBeNull();
		expect(host.querySelector('.assistant-msg .long-message-body')?.classList.contains('collapsed')).toBe(false);
	});

	it('does not add collapse controls to short assistant chat rows', () => {
		mountAssistant('Short reply.');
		expect(host.querySelector('button.long-message-toggle')).toBeNull();
		expect(host.querySelector('.long-message-body')?.classList.contains('collapsed')).toBe(false);
	});

	it('collapses long human user bubbles and expands on request', () => {
		mountUser();
		expectCollapsedThenExpandable('.user-bubble .long-message-body');
	});

	it('collapses long agent-sent user bubbles and keeps attribution visible', () => {
		mountUser({
			sender_kind: 'agent',
			from_thread: 'sender-thread',
			from_title: 'Coordinator',
		});
		expect(host.querySelector('.agent-attr')?.textContent).toContain('Coordinator');
		expectCollapsedThenExpandable('.user-bubble .long-message-body');
	});

	it('collapses long event-chip summaries while keeping the row link clickable', () => {
		mountEvent();
		const chip = host.querySelector<HTMLAnchorElement>('a.ev-chip');
		expect(chip).not.toBeNull();
		expect(chip?.href).toBe('https://github.com/o/r/pull/42#issuecomment-1');
		expectCollapsedThenExpandable('.ev-summary.long-message-body');
	});
});
