import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import DocBottomBand from './DocBottomBand.svelte';
import type { DocComment } from '$lib/stores/docComments';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

// One unread answer with nothing drawn for it: the control points at it (a thread with nothing drawn is a target too).
const unread = { id: 'thread-1', anchor: 'Beta', from: 10, newReplyIds: ['reply-1'], newComment: false } as unknown as DocComment;

function render() {
	host = document.createElement('div');
	document.body.appendChild(host);
	const props = $state({ threads: [unread] as DocComment[], sheetModalOpen: false, onControlGone: vi.fn() });
	component = mount(DocBottomBand, {
		target: host,
		props: {
			artifactId: 'artifact-1', root: host, ready: true, covered: false, onJump: () => undefined,
			get threads() { return props.threads; }, get sheetModalOpen() { return props.sheetModalOpen; }, onControlGone: props.onControlGone,
		},
	});
	flushSync();
	return props;
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

describe('DocBottomBand focus', () => {
	it('puts the focus back in the text when the last unread answer is read while the control holds it', async () => {
		const props = render();
		await vi.waitFor(() => expect(host.querySelector('[data-doc-jump] button')).not.toBeNull());
		host.querySelector<HTMLElement>('[data-doc-jump] button')!.focus();
		props.threads = [];
		flushSync();
		expect(props.onControlGone).toHaveBeenCalledTimes(1);
	});

	it("leaves the focus to the sheet when a phone's sheet opening hides the control", async () => {
		const props = render();
		await vi.waitFor(() => expect(host.querySelector('[data-doc-jump] button')).not.toBeNull());
		host.querySelector<HTMLElement>('[data-doc-jump] button')!.focus();
		props.sheetModalOpen = true;
		flushSync();
		expect(props.onControlGone).not.toHaveBeenCalled();
	});
});
