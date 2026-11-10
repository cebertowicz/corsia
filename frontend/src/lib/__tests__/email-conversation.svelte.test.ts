import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import EmailConversation from '$lib/components/EmailConversation.svelte';
import type { EmailMessage } from '$lib/emailDraft';

const messages: EmailMessage[] = [
	{ id: 'm1', sender: 'Dana Whitfield <dana@example.com>', date: '2026-11-04T17:12:00Z', body: 'Hi Alex,\n\nEight sign-ups so far.' },
	{ id: 'm2', sender: 'Marcus Obi <marcus@example.com>', date: '2026-11-03T09:40:00Z', body: 'I can lead\nthe brush crew.' },
];

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

const toggles = () => [...host.querySelectorAll<HTMLButtonElement>('.email-toggle')];

describe('EmailConversation', () => {
	it('lists the emails collapsed, in the order given, with a count', () => {
		component = mount(EmailConversation, { target: host, props: { messages } });
		expect(host.querySelector('h3')?.textContent).toBe('In this conversation · 2 emails');
		expect(toggles().map((b) => b.querySelector('.email-from')?.textContent)).toEqual(['Dana Whitfield', 'Marcus Obi']);
		expect(toggles().every((b) => b.getAttribute('aria-expanded') === 'false')).toBe(true);
		expect(host.querySelector('.email-body')).toBeNull();
	});

	it('marks the email being replied to, and every preview is one second line', () => {
		component = mount(EmailConversation, { target: host, props: { messages, repliedTo: 'm1' } });
		const [first, second] = toggles();
		expect(first.textContent).toContain('Replying to this email');
		expect(second.textContent).not.toContain('Replying to this email');
		expect(first.querySelector('.email-preview')?.textContent).toBe('Hi Alex, Eight sign-ups so far.');
		expect(second.querySelector('.email-preview')?.textContent).toBe('I can lead the brush crew.');
		expect(host.querySelectorAll('.email-body')).toHaveLength(0);
	});

	it('opens a row on click, shows its address and full text, and closes it again', () => {
		component = mount(EmailConversation, { target: host, props: { messages } });
		const second = toggles()[1];
		second.click();
		flushSync();
		expect(second.getAttribute('aria-expanded')).toBe('true');
		const body = host.querySelector('.email-body');
		expect(body?.id).toBe(second.getAttribute('aria-controls'));
		expect(body?.querySelector('.email-address')?.textContent).toBe('marcus@example.com');
		expect(body?.querySelector('.email-text')?.textContent).toBe('I can lead\nthe brush crew.');
		expect(second.querySelector('.email-preview')).toBeNull();

		second.click();
		flushSync();
		expect(second.getAttribute('aria-expanded')).toBe('false');
		expect(second.querySelector('.email-preview')).not.toBeNull();
	});

	it('expands rows independently', () => {
		component = mount(EmailConversation, { target: host, props: { messages } });
		toggles()[0].click();
		toggles()[1].click();
		flushSync();
		expect(toggles().map((b) => b.getAttribute('aria-expanded'))).toEqual(['true', 'true']);
	});

	it('says "1 email" for a single message', () => {
		component = mount(EmailConversation, { target: host, props: { messages: messages.slice(0, 1) } });
		expect(host.querySelector('h3')?.textContent).toContain('1 email');
		expect(host.querySelector('h3')?.textContent).not.toContain('1 emails');
	});
});

describe('EmailConversation with newer emails', () => {
	const newer: EmailMessage[] = [
		{ id: 'n2', sender: 'Dana Whitfield <dana@example.com>', date: '2026-11-06T18:05:00Z', body: 'Third chipper.' },
		{ id: 'n1', sender: 'Marcus Obi <marcus@example.com>', date: '2026-11-05T17:40:00Z', body: 'The gate code changed.' },
	];

	it('lists the newer emails above the one being replied to, collapsed like the rest, with no badge or link', () => {
		component = mount(EmailConversation, { target: host, props: { messages: [...newer, ...messages], repliedTo: 'm1' } });
		expect(host.querySelector('h3')?.textContent).toBe('In this conversation · 4 emails');
		expect(toggles().map((b) => b.querySelector('.email-from')?.textContent)).toEqual(['Dana Whitfield', 'Marcus Obi', 'Dana Whitfield', 'Marcus Obi']);
		expect(toggles().map((b) => b.textContent?.includes('Replying to this email'))).toEqual([false, false, true, false]);
		expect(toggles().every((b) => b.getAttribute('aria-expanded') === 'false')).toBe(true);
		expect(host.querySelectorAll('a')).toHaveLength(0);
		expect(host.textContent).not.toMatch(/newer/i);
	});
});

describe('EmailConversation read states', () => {
	it('shows only the label and a status while loading', () => {
		component = mount(EmailConversation, { target: host, props: { messages: [], status: 'loading' } });
		expect(host.querySelector('h3')?.textContent).toBe('In this conversation');
		expect(host.querySelector('[role="status"]')?.textContent).toContain('Loading the earlier emails');
		expect(toggles()).toHaveLength(0);
	});

	it('says a failed read failed and offers Retry, which calls back once per press', () => {
		let tries = 0;
		component = mount(EmailConversation, { target: host, props: { messages: [], status: 'error', onretry: () => { tries += 1; } } });
		expect(host.querySelector('[role="alert"]')?.textContent).toContain('Couldn’t load the earlier emails in this conversation.');
		const retry = host.querySelector<HTMLButtonElement>('.email-retry')!;
		expect(retry.textContent).toBe('Retry');
		retry.click();
		expect(tries).toBe(1);
	});

	it('a partial read lists the emails it has, counts the missing ones and offers Retry', () => {
		component = mount(EmailConversation, { target: host, props: { messages, status: 'partial', unreadable: 3 } });
		expect(host.querySelector('h3')?.textContent).toContain('2 emails');
		expect(host.querySelector('[role="alert"]')?.textContent).toContain('3 emails in this conversation couldn’t be loaded.');
		expect(toggles()).toHaveLength(2);
		expect(host.querySelector('.email-retry')).not.toBeNull();
	});

	it('says "1 email" for a single unreadable email', () => {
		component = mount(EmailConversation, { target: host, props: { messages, status: 'partial', unreadable: 1 } });
		expect(host.querySelector('[role="alert"]')?.textContent).toContain('1 email in this conversation');
	});
});
