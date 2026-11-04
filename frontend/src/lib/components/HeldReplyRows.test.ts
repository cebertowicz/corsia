/** The rows a held reply leaves in a transcript: its outcome receipts and the asker's waiting note. */
import { afterEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import EventChip from './EventChip.svelte';
import SystemMessageRow from './SystemMessageRow.svelte';

let host: HTMLElement | null = null;
let instance: Record<string, unknown> | null = null;

function render(component: typeof EventChip | typeof SystemMessageRow, msg: Record<string, unknown>): HTMLElement {
	host = document.createElement('div');
	document.body.appendChild(host);
	instance = mount(component as never, { target: host, props: { msg } });
	flushSync();
	return host;
}

afterEach(() => {
	if (instance) unmount(instance);
	instance = null;
	host?.remove();
	host = null;
});

const LONG = 'Couldn’t send the reply because the Researcher agent no longer exists, so this line is long enough to wrap on a phone and must never be cut short or hidden behind a Show more control, however narrow the screen is.';

describe('held reply receipts', () => {
	it.each([
		['held_reply.sent', 'Sent'],
		['held_reply.declined', 'Declined'],
		['held_reply.not_sent', 'Not sent'],
		['held_reply.queued', 'Queued'],
	])('%s reads %s', (eventType, word) => {
		const el = render(EventChip, {
			id: 'r1', role: 'system', content: 'agent text',
			metadata: { type: 'context_event', event_type: eventType, summary: 'The line.' },
		});
		expect(el.querySelector('.ev-label')?.textContent).toBe(word);
		expect(el.querySelector('.ev-summary')?.textContent).toBe('The line.');
	});

	it('wraps a long line and offers no Show more', () => {
		const el = render(EventChip, {
			id: 'r2', role: 'system', content: LONG,
			metadata: { type: 'context_event', event_type: 'held_reply.not_sent', summary: LONG },
		});
		expect(el.querySelector('.ev-chip')?.classList.contains('held')).toBe(true);
		expect(el.querySelector('.ev-more')).toBeNull();
		expect(el.querySelector('.ev-summary')?.classList.contains('collapsed')).toBe(false);
	});
});

describe('held reply note', () => {
	it("shows the person's line, never the text the asker's agent reads", () => {
		const el = render(SystemMessageRow, {
			id: 'n1', role: 'system',
			content: "Sender's reply is waiting for the person's approval; do not ask again.",
			metadata: { type: 'held_reply_note', summary: "Sender's reply is waiting for approval.", held_reply_id: 'h1' },
		});
		expect(el.textContent?.trim()).toBe("Sender's reply is waiting for approval.");
	});
});
