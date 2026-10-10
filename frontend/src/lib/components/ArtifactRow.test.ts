/**
 * ArtifactRow.test.ts
 *
 * The row's external-link activation: an opener that refuses must say so through
 * the same toast the markdown link path shows, and a successful open stays silent.
 */

import { get } from 'svelte/store';
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { toasts } from '$lib/stores/toast';
import { openExternalUrl } from '$lib/linkTargetResolver';
import ArtifactRow from './ArtifactRow.svelte';

vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn() }));

function externalLinkNode(overrides: Record<string, unknown> = {}) {
	return {
		kind: 'artifact', id: 'a1', integration: 'github', type: 'pull-request',
		external_id: 'acme/widget#12', title: null, name: 'widget#12', path: null,
		retention: 'durable', surfacing: 'visible', pinned: false, pin_eligible: false,
		retirement_exempt: false, state: 'active', updated_at: null, description: null,
		metadata: {},
		presentation: { label: 'Pull request', icon: 'git-pull-request', capability: 'external-link' },
		action: { capability: 'external-link', disabled: false, disabled_reason: null },
		...overrides,
	};
}

const HREF = 'https://github.com/acme/widget/pull/12';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function clearToasts() {
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
}

beforeEach(() => {
	clearToasts();
	vi.mocked(openExternalUrl).mockReset();
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	clearToasts();
});

function mountRow(item: Record<string, unknown>) {
	component = mount(ArtifactRow, {
		target: host,
		props: { item: item as any, onHide: vi.fn(), onPin: vi.fn() },
	});
	flushSync();
}

async function clickOpenableRow() {
	const row = host.querySelector<HTMLButtonElement>('button.row.openable');
	expect(row).toBeTruthy();
	row!.click();
	// Let a rejected opener settle before the feedback is read.
	await new Promise((resolve) => setTimeout(resolve, 0));
	flushSync();
}

describe('ArtifactRow external links', () => {
	it('opens the resolved pull-request URL and stays quiet', async () => {
		vi.mocked(openExternalUrl).mockResolvedValue(undefined);
		mountRow(externalLinkNode());

		await clickOpenableRow();

		expect(openExternalUrl).toHaveBeenCalledWith(HREF);
		expect(get(toasts)).toHaveLength(0);
	});

	it('reports a refused opener instead of swallowing the rejection', async () => {
		vi.mocked(openExternalUrl).mockRejectedValue(new Error('no browser handler'));
		mountRow(externalLinkNode());

		await clickOpenableRow();

		expect(openExternalUrl).toHaveBeenCalledWith(HREF);
		expect(get(toasts).map((toast) => [toast.message, toast.type])).toEqual([
			["Couldn't open the external link", 'error'],
		]);
	});

	it('leaves an unresolvable external link disabled and never calls the opener', async () => {
		mountRow(externalLinkNode({ external_id: 'not-a-pr-id' }));

		expect(host.querySelector('button.row.openable')).toBeNull();
		expect(openExternalUrl).not.toHaveBeenCalled();
		expect(get(toasts)).toHaveLength(0);
	});

	it('shows the passive reply glyph with its accessible name when waiting', () => {
		mountRow(externalLinkNode({ reply_waiting: true }));

		const glyph = host.querySelector('.reply-mark');
		expect(glyph?.getAttribute('role')).toBe('img');
		expect(glyph?.getAttribute('aria-label')).toBe('Reply waiting');
	});

	it('does not show Reply when no response is waiting', () => {
		mountRow(externalLinkNode());

		expect(host.querySelector('.reply-mark')).toBeNull();
	});
});

describe('ArtifactRow opens an artifact the backend typed as an image', () => {
	const fileNode = (type: string, path: string) => externalLinkNode({
		type, path, integration: 'workspace', external_id: path, name: path.split('/').pop(),
		presentation: { label: 'Image', icon: 'image', capability: 'leaf-package' },
		action: { capability: 'leaf-package', disabled: false, disabled_reason: null },
	});

	async function openRow(type: string, path: string, threadId: string) {
		component = mount(ArtifactRow, {
			target: host,
			props: { item: fileNode(type, path) as any, onHide: vi.fn(), onPin: vi.fn(), threadId },
		});
		flushSync();
		await clickOpenableRow();
		unmount(component);
		component = null;
	}

	it('hands the type to the tab, and a path alone carries nothing', async () => {
		const { threadWorkspaces } = await import('$lib/stores/artifactWorkspace');
		threadWorkspaces.set({});
		await openRow('image', '/ws/shot.png', 't-image');
		await openRow('code', '/ws/legacy.png', 't-code');
		expect(get(threadWorkspaces)['t-image'].tabs[0].isImage).toBe(true);
		expect(get(threadWorkspaces)['t-code'].tabs[0].isImage).toBeUndefined();
	});
});
