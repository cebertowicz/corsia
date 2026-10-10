import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import type { ServerComment } from '$lib/api/docs';
import { displayedDocument } from '$lib/ws/presence';
import { docComments } from '$lib/stores/docComments';
import { recordDocActivity, resetDocPresenceForTests } from '$lib/docPresence';
import { startDocPresence } from '$lib/docPresenceRuntime';
import { commentHoldMs, selectSweepMs } from '$lib/typingPace';
import { createTypedReveal, typedReveals } from '$lib/cm/typed-reveal';
import { previewSlot } from '$lib/cm/typed-reveal-preview';
import DocPresenceStatus from './DocPresenceStatus.svelte';

const wire = vi.hoisted(() => ({ rows: [] as unknown[] }));
vi.mock('$lib/api/docs', async (original) => ({
	...await original<typeof import('$lib/api/docs')>(),
	fetchComments: async () => ({
		comments: wire.rows, feedback: { open_count: 0, owner_label: null, grace_closes_at: null, undelivered_batches: [] },
	}),
}));

const ART = 'art';
const DOC = 'An opening line, then the anchored words, then the rest of it.';
const ANCHOR = { from: DOC.indexOf('anchored'), to: DOC.indexOf('anchored') + 'anchored words'.length };
const BODY = 100;
/** Sweep over the anchor, then the body's hold: both counted from when the highlight began. */
const CARD_AT = selectSweepMs(ANCHOR.to - ANCHOR.from) + commentHoldMs(BODY);

let component: ReturnType<typeof mount> | undefined;
let stop: (() => void) | undefined;
let view: EditorView;

function serverComment(id: string): ServerComment {
	return {
		id, artifact_id: ART, parent_id: null, author: 'agent', author_kind: 'agent', body: 'c'.repeat(BODY),
		anchor_text: 'anchored words', from_off: ANCHOR.from, to_off: ANCHOR.to, review: 0, resolved: 0, acked: 0, orphaned: false,
		created_at: '2026-10-10T20:00:00Z', updated_at: '2026-10-10T20:00:00Z', replies: [],
	};
}

/** The body is still being written: the server streams the comment call's anchor. */
function streamAnchor() {
	view.dispatch({ effects: previewSlot.of({
		callId: 'call-1', threadId: 'agent-a', name: 'Alpha', slot: 0, ...ANCHOR, text: '', mode: 'comment',
	}) });
}

/** The call committed: the comment exists and its activity frame names the call. */
function commit(callId: string | null = 'call-1') {
	wire.rows = [serverComment('c1')];
	recordDocActivity({
		type: 'doc_activity', artifact_id: ART, thread_id: 'agent-a', agent: 'Alpha', kind: 'comment',
		span: ANCHOR, comment_id: 'c1', revision: null, at: new Date().toISOString(), call_id: callId,
	});
}

const shown = () => (get(docComments)[ART] ?? []).map((row) => row.id);
const selectedText = () => [...view.dom.querySelectorAll('.cm-presence-select')].map((el) => el.textContent).join('');

beforeEach(() => {
	vi.useFakeTimers();
	vi.setSystemTime(new Date('2026-10-10T20:00:00Z'));
	resetDocPresenceForTests();
	wire.rows = [];
	docComments.set({});
	displayedDocument.set({ threadId: 'owner', value: { canShow: true, showing: ART } });
	const root = document.createElement('div');
	document.body.append(root);
	view = new EditorView({
		state: EditorState.create({ doc: DOC, extensions: [createTypedReveal({ canType: () => true, isInView: () => true })] }),
		parent: root,
	});
	stop = startDocPresence();
	component = mount(DocPresenceStatus, {
		target: document.body,
		props: {
			target: { instanceId: 'tab', path: 'p.md', name: 'p.md', projectId: null, threadId: 'owner', artifactId: ART },
			artifactId: ART, docState: 'ready', root, onPrepareCleanReload: async () => null, onFeedback: () => {},
		},
	});
	flushSync();
});

afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	stop?.();
	view.destroy();
	vi.useRealTimers();
	resetDocPresenceForTests();
	document.body.innerHTML = '';
});

describe('a streamed comment in the document canvas', () => {
	it('highlights the anchor while the body is written, and the card shows on commit after the hold from the highlight', async () => {
		streamAnchor();
		await vi.advanceTimersByTimeAsync(400);
		expect(selectedText()).toBe('anchored words');
		commit();
		await vi.advanceTimersByTimeAsync(CARD_AT - 400 - 50);
		expect(shown()).toEqual([]);
		expect(selectedText()).toBe('anchored words');
		await vi.advanceTimersByTimeAsync(100);
		expect(shown()).toEqual(['c1']);
		expect(selectedText()).toBe('');
		expect(view.state.field(typedReveals).flatMap((lane) => lane.previews)).toEqual([]);
	});

	it('a commit after a slow body shows the card at once: the hold already ran during the writing', async () => {
		streamAnchor();
		await vi.advanceTimersByTimeAsync(CARD_AT + 5_000);
		expect(selectedText()).toBe('anchored words');
		commit();
		await vi.advanceTimersByTimeAsync(50);
		expect(shown()).toEqual(['c1']);
	});
});
