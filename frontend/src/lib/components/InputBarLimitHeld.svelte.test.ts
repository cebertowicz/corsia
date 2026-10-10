import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import InputBarControls from './InputBarControls.svelte';
import { token } from '$lib/stores/auth';
import { activeThreadId, threads } from '$lib/stores/threads';
import { activeModelId, models } from '$lib/stores/models';
import { withAccountTurn } from '$lib/stores/modelFixtures';

// The held line mounts beside the composer so it opens the chip's own picker.
let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	token.set('test-token');
	vi.stubGlobal('fetch', vi.fn(() => new Promise<never>(() => {})));
	activeThreadId.set('thread-held');
	threads.set([{ id: 'thread-held', title: 'Held' } as never]);
	activeModelId.set('codex/gpt-5-codex');
	models.set([withAccountTurn({
		id: 'codex/gpt-5-codex', name: 'gpt', connection: 'codex', size: '', backend: 'harness', speed: '',
		status: 'unavailable', status_reason: 'usage_limited',
		usage: { state: 'limited', usable: false, reason: 'plan_limit_overage_off', resets_at: null, windows: [] },
	})]);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
	token.set(null);
	activeThreadId.set(null);
	threads.set([]);
	activeModelId.set(null);
	models.set([]);
});

describe('held line in the composer', () => {
	it('"Switch model" opens the model sheet the chip opens', () => {
		const toggleSheet = vi.fn();
		component = mount(InputBarControls, {
			target: host,
			props: {
				surface: 'sheets', sheetView: null, toggleSheet, isMobile: false, hasImageAttachments: false,
				inputText: '', pendingAttachmentCount: 0, hasMessageToSend: false, canSendToModel: false, queueUntilSwitch: false,
				onAttachFile: () => {}, onAttachCamera: () => {}, onAttachPhotos: () => {}, onSendClick: () => {},
				onQueueAfterTurn: () => {},
			},
		}) as Record<string, unknown>;
		flushSync();
		expect(host.querySelector('.limit-held-text')?.textContent).toBe('Plan limit reached · reset time unknown');

		const button = [...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Switch model');
		button!.click();
		expect(toggleSheet).toHaveBeenCalledExactlyOnceWith('model');
	});
});
