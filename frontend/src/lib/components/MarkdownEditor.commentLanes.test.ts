import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { fromStore, writable } from 'svelte/store';
import { EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import { withLiveUpdateAuthor } from '$lib/stores/liveUpdateAuthor';
import type { PresenceRange } from '$lib/cm/presence-marks';
import { typedReveals } from '$lib/cm/typed-reveal';
import { hiddenCount } from '$lib/cm/typed-reveal-segments';
import MarkdownEditor from './MarkdownEditor.svelte';

const A = { threadId: 'agent-a', name: 'Alpha' };
const B = { threadId: 'agent-b', name: 'Beta' };
const HEAD = 'Start. ';
const TAIL = 'End.';
const TYPED = 'The quick brown fox jumps over the lazy dog. '.repeat(3);

let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;
const presence = writable<PresenceRange[]>([]);
let doc = new Y.Doc();

function mountEditor(): EditorView {
	const text = doc.getText('text');
	text.insert(0, HEAD + TAIL);
	const live = fromStore(writable(text));
	const ranges = fromStore(presence);
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(MarkdownEditor, {
		target: host,
		props: {
			value: text.toString(), typingAllowed: true,
			get liveText() { return live.current; },
			get presence() { return ranges.current; },
		},
	});
	flushSync();
	return EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
}

/** Agent A's edit arrives from the server and starts typing at its caret. */
function typeAsAgentA() {
	const remote = new Y.Doc();
	Y.applyUpdate(remote, Y.encodeStateAsUpdate(doc));
	const before = Y.encodeStateVector(remote);
	remote.getText('text').insert(HEAD.length, TYPED);
	withLiveUpdateAuthor(A, () => Y.applyUpdate(doc, Y.encodeStateAsUpdate(remote, before), 'server'));
	flushSync();
}

const hidden = (view: EditorView) => hiddenCount(view.state.field(typedReveals)[0]?.segments ?? []);
const mark = (selector: string, thread: string) => host.querySelector(selector + '[data-thread-id="' + thread + '"]');

function commentAnchor(thread: typeof A, from: number, to: number, kind: 'comment' | 'edit' = 'comment'): PresenceRange {
	return {
		threadId: thread.threadId, name: thread.name, color: '#8b5cf6', span: { from, to },
		highlightSpan: { from, to }, highlightKind: kind,
	};
}

beforeEach(() => {
	vi.useFakeTimers();
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue(null);
	// jsdom has no layout, so nothing is "on screen" unless the viewport is stated (the agent types mid-document).
	vi.spyOn(EditorView.prototype, 'visibleRanges', 'get').mockReturnValue([{ from: 0, to: HEAD.length + TAIL.length }]);
	presence.set([]);
	doc = new Y.Doc();
});

afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	doc.destroy();
	vi.restoreAllMocks();
	vi.useRealTimers();
});

describe('comment anchors over text an agent is still typing', () => {
	it('row 8: another agent comment anchor shows the typed text through the anchor end, then highlights', () => {
		const view = mountEditor();
		typeAsAgentA();
		expect(hidden(view)).toBe(TYPED.length);
		presence.set([commentAnchor(B, HEAD.length + 4, HEAD.length + 20)]);
		flushSync();
		expect(hidden(view)).toBe(TYPED.length - 20);
		expect(mark('.cm-presence-arrival', B.threadId)).not.toBeNull();
		expect(mark('.cm-presence-cursor', B.threadId)).not.toBeNull();
		expect(mark('.cm-presence-cursor', A.threadId)).not.toBeNull();
	});

	it('row 29: the typing agent own comment anchor reveals its own span and still highlights', () => {
		const view = mountEditor();
		typeAsAgentA();
		presence.set([commentAnchor(A, HEAD.length, HEAD.length + 30)]);
		flushSync();
		expect(hidden(view)).toBe(TYPED.length - 30);
		expect(mark('.cm-presence-arrival', A.threadId)).not.toBeNull();
	});

	it('an edit highlight never cuts the typing short', () => {
		const view = mountEditor();
		typeAsAgentA();
		presence.set([commentAnchor(B, HEAD.length + 4, HEAD.length + 20, 'edit')]);
		flushSync();
		expect(hidden(view)).toBe(TYPED.length);
	});

	it('row 9a: clicking a pending anchor asks for its comment at once', () => {
		const view = mountEditor();
		const revealPending = vi.fn();
		presence.set([{ ...commentAnchor(B, 0, 5), revealPending }]);
		flushSync();
		view.dom.querySelector<HTMLElement>('.cm-presence-arrival')!.click();
		expect(revealPending).toHaveBeenCalledTimes(1);
	});
});
