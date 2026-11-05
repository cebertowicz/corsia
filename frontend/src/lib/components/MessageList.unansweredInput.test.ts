import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it } from 'vitest';
import MessageList from './MessageList.svelte';
import { activeThreadId, messages } from '$lib/stores/threads';

it('renders a paired retry as one error notice with no event row', () => {
	const host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set('retry-thread');
	messages.set([
		{ id: 'error', thread_id: 'retry-thread', role: 'system', content: 'Provider failed.', created_at: '2026-01-01T00:00:00Z', metadata: { type: 'llm_error', turn_id: 'failed' } },
		{ id: 'wake', thread_id: 'retry-thread', role: 'system', content: 'The agent tried once more.', created_at: '2026-01-01T00:00:01Z', metadata: { type: 'activity_receipt', event_type: 'system.turn.unanswered_input', failed_turn_id: 'failed', summary: 'The agent tried once more.' } },
	]);
	const component = mount(MessageList, { target: host, props: { messagesEl: undefined, userIsNearBottom: true, ignoreScrollEvents: false, onOpenLightbox: () => {} } });
	flushSync();
	expect(host.textContent).toContain('Provider failed. The agent tried once more.');
	expect(host.querySelector('.ev-chip')).toBeNull();
	unmount(component);
	host.remove();
});

afterEach(() => { activeThreadId.set(null); messages.set([]); });
