import { flushSync, mount, unmount } from 'svelte';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DocActivityFrame } from '$lib/docPresence';
import type { DocPresenceSurface } from '$lib/docPresenceRuntime';

const attached: { surface: DocPresenceSurface | null } = { surface: null };
vi.mock('$lib/docPresenceRuntime', () => ({
	attachDocPresenceSurface: (_artifact: string, _id: string, surface: DocPresenceSurface) => { attached.surface = surface; return () => { attached.surface = null; }; },
	notifyDocPresenceViewingChanged: () => {},
}));
const comment = { id: 'c1', text: 'x'.repeat(100), from: 4, to: 15, orphaned: false };
vi.mock('$lib/stores/docCommentArrivals', () => ({
	prepareCommentArrival: async () => ({ comment, feedback: {} }),
	revealCommentArrival: () => {},
}));

import DocPresenceStatus from './DocPresenceStatus.svelte';
import { createTypedReveal, typedReveals } from '$lib/cm/typed-reveal';
import { presenceMarks } from '$lib/cm/presence-marks';
import { commentHoldMs, selectSweepMs } from '$lib/typingPace';

const frame: DocActivityFrame = {
	type: 'doc_activity', artifact_id: 'artifact-1', thread_id: 'agent-1', agent: 'Docs', kind: 'comment',
	span: { from: 4, to: 15 }, comment_id: 'c1', revision: 'r1', at: '2026-09-30T05:00:00Z',
};
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let view: EditorView | null = null;

function render(root: HTMLElement | null) {
	component = mount(DocPresenceStatus, {
		target: host,
		props: {
			target: { threadId: 'thread-1', instanceId: 'tab-1' } as never, artifactId: 'artifact-1', docState: 'ready', root,
			onPrepareCleanReload: async () => null, onFeedback: () => {},
		},
	});
	flushSync();
}
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); });
afterEach(() => { if (component) unmount(component); component = null; view?.destroy(); view = null; host.remove(); attached.surface = null; });

describe('presence surface hands a comment arrival to the editor it sits beside', () => {
	it('asks the mounted editor for one select keyed by the comment, sweep by anchor size, hold by body size', async () => {
		const root = document.createElement('div'); document.body.appendChild(root);
		view = new EditorView({ state: EditorState.create({ doc: 'Say hello brave world.', extensions: [presenceMarks, createTypedReveal({ canType: () => true })] }), parent: root });
		render(root);
		const prepared = await attached.surface!.prepare(frame);
		const running = attached.surface!.select!(frame, prepared);
		expect(running).not.toBeNull();
		expect(view.state.field(typedReveals)[0].select).toMatchObject({
			key: 'c1', from: 4, to: 15, sweepMs: selectSweepMs(11), holdMs: commentHoldMs(100),
		});
		root.remove();
	});

	it('cannot run a select with no editor mounted, so the queue keeps its own delay', async () => {
		render(null);
		const prepared = await attached.surface!.prepare(frame);
		expect(attached.surface!.select!(frame, prepared)).toBeNull();
	});
});
