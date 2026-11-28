import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import EmailAttachments from './EmailAttachments.svelte';
import type { EmailAttachment } from '$lib/emailDraft';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

describe('EmailAttachments', () => {
	it('shows one chip per file, even when two files share a name', () => {
		const attachments: EmailAttachment[] = [
			{ name: 'waiver.pdf', path: '/w/2025/waiver.pdf', size: 2048, type: 'document' },
			{ name: 'waiver.pdf', path: '/w/2026/waiver.pdf', size: 4096, type: 'document' },
		];
		host = document.body.appendChild(document.createElement('div'));
		component = mount(EmailAttachments, { target: host, props: { attachments, threadId: 'thread-1' } });
		flushSync();
		const chips = [...host.querySelectorAll('li.attachment')];
		expect(chips).toHaveLength(2);
		expect(chips.map((chip) => chip.textContent)).toEqual([expect.stringContaining('2 KB'), expect.stringContaining('4 KB')]);
	});

	it('offers separate removal for unavailable and available chips, including duplicates', async () => {
		const removed: Array<[number, string]> = [];
		const attachments: EmailAttachment[] = [
			{ name: 'map.pdf', path: '/w/map.pdf', size: 2048, type: 'document', unavailable: true },
			{ name: 'map.pdf', path: '/w/map.pdf', size: 2048, type: 'document' },
		];
		host = document.body.appendChild(document.createElement('div'));
		component = mount(EmailAttachments, { target: host, props: {
			attachments, threadId: 'thread-1', removable: true,
			onremove: async (index: number, path: string) => { removed.push([index, path]); },
		} });
		flushSync();
		const buttons = host.querySelectorAll<HTMLButtonElement>('button[aria-label="Remove map.pdf"]');
		expect(buttons).toHaveLength(2);
		buttons[1].click();
		await Promise.resolve();
		expect(removed).toEqual([[1, '/w/map.pdf']]);
	});

	it('still shows every chip when the server could not place the files (no paths)', () => {
		const attachments: EmailAttachment[] = [
			{ name: 'map.png', path: '', size: 2048, type: 'image', unavailable: true },
			{ name: 'waiver.pdf', path: '', size: 4096, type: 'document', unavailable: true },
		];
		host = document.body.appendChild(document.createElement('div'));
		component = mount(EmailAttachments, { target: host, props: { attachments, threadId: 'thread-1' } });
		flushSync();
		expect(host.querySelectorAll('li.attachment')).toHaveLength(2);
	});
});
