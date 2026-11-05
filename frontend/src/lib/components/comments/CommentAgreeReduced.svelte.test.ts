import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { writable } from 'svelte/store';

const calls = vi.hoisted(() => ({ capture: vi.fn(), play: vi.fn() }));
vi.mock('$lib/stores/viewport', async (importOriginal) => ({ ...await importOriginal<object>(), prefersReducedMotion: writable(true) }));
vi.mock('$lib/actions/layoutCrossfade', async (importOriginal) => ({ ...await importOriginal<object>(), captureLayout: calls.capture, playLayoutCrossfade: calls.play }));
import CommentThread from './CommentThread.svelte';

let host: HTMLElement;
let component: Record<string, unknown>;
afterEach(() => { if (component) unmount(component); host?.remove(); vi.clearAllMocks(); });

describe('Agree reduced motion', () => {
	it('uses the shared layout crossfade when the agreed line changes', async () => {
		const overlay = document.createElement('div');
		calls.capture.mockReturnValue({ overlay, height: 1, scroll: [] });
		calls.play.mockReturnValue({ cancel: vi.fn(), handoff: vi.fn(), supersede: vi.fn() });
		const comment = $state({ id: 'c', anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, acked: false, replies: [] });
		host = document.createElement('div'); document.body.append(host);
		component = mount(CommentThread, { target: host, props: { comment } }); flushSync();
		comment.acked = true; flushSync(); await tick();
		expect(calls.capture).toHaveBeenCalledOnce();
		expect(calls.play).toHaveBeenCalledOnce();
	});
});
