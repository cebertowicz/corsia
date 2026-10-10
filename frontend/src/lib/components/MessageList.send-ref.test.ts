import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import {
	activeThreadId,
	activeToolsByThread,
	messages,
} from '$lib/stores/threads';
import { openSubagentRequest } from '$lib/stores/subagents';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { coreSettings } from '$lib/stores/settings';
import MessageList from './MessageList.svelte';

const THREAD_ID = 'sender-thread';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set(THREAD_ID);
	_resetTurnStateForTests();
	activeToolsByThread.set(new Map());
	openSubagentRequest.set(null);
	coreSettings.set({ features: { focus_mode: false } } as any);
	messages.set([
		{
			id: 'send-ref-1',
			thread_id: THREAD_ID,
			role: 'system',
			content: 'Sent to Worker Target',
			created_at: '2026-06-18T00:00:00',
			metadata: {
				type: 'send_ref',
				target_thread_id: 'target-thread',
				target_title: 'Worker Target',
				target_kind: 'subagent',
			},
		},
	]);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	openSubagentRequest.set(null);
	messages.set([]);
	coreSettings.set(null);
});

function renderList() {
	component = mount(MessageList, {
		target: host,
		props: {
			messagesEl: undefined,
			userIsNearBottom: true,
			ignoreScrollEvents: false,
			onOpenLightbox: () => {},
		},
	});
	flushSync();
}

describe('MessageList send_ref display', () => {
	it('renders a clickable sender-side send breadcrumb', () => {
		renderList();

		const chip = host.querySelector<HTMLButtonElement>('button.send-chip');
		expect(chip).not.toBeNull();
		expect(chip?.textContent).toContain('Sent');
		expect(chip?.textContent).toContain('Worker Target');

		chip?.click();
		flushSync();
		expect(get(openSubagentRequest)).toBe('target-thread');
	});

	it('renders CLI-shaped agent sends as distinct left-side inbound rows', () => {
		messages.set([
			{
				id: 'agent-user-1',
				thread_id: THREAD_ID,
				role: 'user',
				content: 'This is a test via /send endpoint. It should not look like a user-authored bubble.',
				created_at: '2026-06-18T00:00:00',
				metadata: {
					sender_kind: 'agent',
					from_thread: 'sender-thread',
					from_title: 'Project Coordinator',
					send_ref: { source: 'cli' },
				},
			},
		]);

		renderList();

		const row = host.querySelector<HTMLElement>('.message.user');
		expect(row).not.toBeNull();
		expect(row?.classList.contains('agent-inbound')).toBe(true);
		expect(row?.classList.contains('human-user')).toBe(false);
		expect(host.querySelector('.agent-attr')?.textContent).toContain('From Project Coordinator');
		expect(host.querySelector('[data-testid="edit-button"]')).toBeNull();
	});
});
