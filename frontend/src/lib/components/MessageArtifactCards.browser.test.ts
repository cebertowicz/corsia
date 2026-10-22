import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import MessageArtifactCards from './MessageArtifactCards.svelte';
import type { MessageCard } from '$lib/api/messageCards';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { paneOpen } from '$lib/stores/paneOpen';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const browser = { host: 'github.com' };
const file: MessageCard = {
	kind: 'artifact', artifact_id: 'a1', title: 'notes.md', type: 'document', icon: 'file-text', name: 'notes.md',
	path: 'notes.md', available: true, opens: true,
};

function render(props: Record<string, unknown>): void {
	if (component) unmount(component);
	component = mount(MessageArtifactCards, { target: host, props: { threadId: 't1', cards: [], ...props } });
	flushSync();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	paneOpen.set(false);
	threadWorkspaces.set({});
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

describe('MessageArtifactCards browser chip', () => {
	it('draws the chip in the file chip strip, after the files, as its own control and not as a file', () => {
		render({ cards: [file], browser });

		const chips = [...host.querySelectorAll<HTMLElement>('ul.chips button.tile.chip')];
		expect(chips.map((chip) => chip.querySelector('.title')?.textContent)).toEqual(['notes.md', 'github.com']);
		expect(chips[1].getAttribute('title')).toBe('Reopen the shared browser');
		expect(host.querySelectorAll('[data-artifact-id]')).toHaveLength(1);
		expect(host.querySelector('ul.chips')?.getAttribute('aria-label')).toBe('Files and shared browser');
	});

	it('stands alone in a strip that is not called Artifacts when the message names no files', () => {
		render({ cards: [], browser });

		expect(host.querySelectorAll('button.tile.chip')).toHaveLength(1);
		expect(host.querySelector('ul.chips')?.getAttribute('aria-label')).toBe('Shared browser');
	});

	it('reopens the browser in the message thread, without opening an artifact', () => {
		render({ cards: [], browser, threadId: 'browser-thread' });

		host.querySelector<HTMLElement>('button.tile.chip')!.click();

		const ws = get(threadWorkspaces)['browser-thread'];
		expect(ws.tabs.map((tab) => tab.kind)).toEqual(['browser']);
	});

	it('draws no chip without a thread to reopen, or without a browser open', () => {
		render({ cards: [file], browser, threadId: null });
		expect(host.querySelectorAll('.tile')).toHaveLength(1);

		render({ cards: [file], browser: null });
		expect(host.querySelectorAll('.tile')).toHaveLength(1);
		expect(host.querySelector('ul.chips')?.getAttribute('aria-label')).toBe('Files');
	});
});
