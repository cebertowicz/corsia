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
});
