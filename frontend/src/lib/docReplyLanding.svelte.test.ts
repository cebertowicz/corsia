import { expect, it, vi } from 'vitest';
import { flushSync, tick } from 'svelte';
import { createReplyNavigation } from './docReplyLanding.svelte';
import type { DocComment } from '$lib/stores/docComments';
import type { DocViewerTarget } from '$lib/docViewerTarget';

const unread = { id: 'c', anchor: 'quote', author: 'agent', text: 'note', at: '', resolved: false, replies: [], newComment: true } as unknown as DocComment;

it('shows the first unread thread on a restored tab without reading it, and reads the one the jump control goes to', async () => {
	const show = vi.fn();
	let goTo!: (id: string) => void;
	const stop = $effect.root(() => {
		({ goTo } = createReplyNavigation({
			target: () => ({ instanceId: 'tab', landOn: 'new-replies' }) as unknown as DocViewerTarget,
			ready: () => true, comments: () => [unread], text: () => 'quote',
			show, openImageOr: (_id, otherwise) => otherwise(), onLanded: () => {},
		}));
	});
	flushSync();
	await tick();
	expect(show).toHaveBeenCalledTimes(1);
	expect(show.mock.lastCall?.[0]).toBe(unread);
	expect(show.mock.lastCall?.[2]).toBe(false);
	goTo('c');
	expect(show).toHaveBeenCalledTimes(2);
	expect(show.mock.lastCall?.[0]).toBe(unread);
	expect(show.mock.lastCall?.[2]).toBe(true);
	stop();
});
