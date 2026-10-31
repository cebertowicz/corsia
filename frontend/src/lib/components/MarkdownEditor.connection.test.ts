import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { fromStore, writable } from 'svelte/store';
import { EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import { withLiveUpdateAuthor } from '$lib/stores/liveUpdateAuthor';
import { typedReveals } from '$lib/cm/typed-reveal';
import { hiddenCount } from '$lib/cm/typed-reveal-segments';
import MarkdownEditor from './MarkdownEditor.svelte';

const A = { threadId: 'agent-a', name: 'Alpha' };
const HEAD = 'Start. ';
const TAIL = 'End.';
const TYPED = 'The quick brown fox jumps over the lazy dog. '.repeat(3);

let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;
let doc = new Y.Doc();
const connected = writable(true);
const liveText = writable<Y.Text>(new Y.Text());

function mountEditor(): EditorView {
	const text = doc.getText('text');
	text.insert(0, HEAD + TAIL);
	liveText.set(text);
	const live = fromStore(liveText);
	const typing = fromStore(connected);
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(MarkdownEditor, {
		target: host,
		props: {
			value: text.toString(),
			get typingAllowed() { return typing.current; },
			get liveText() { return live.current; },
		},
	});
	flushSync();
	return EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
}

/** Agent A's edit arrives from the server, as it does on a live connection or in the sync that follows a reconnect. */
function agentATypes() {
	const remote = new Y.Doc();
	Y.applyUpdate(remote, Y.encodeStateAsUpdate(doc));
	const before = Y.encodeStateVector(remote);
	remote.getText('text').insert(HEAD.length, TYPED);
	withLiveUpdateAuthor(A, () => Y.applyUpdate(doc, Y.encodeStateAsUpdate(remote, before), 'server'));
	flushSync();
}

const hidden = (view: EditorView) => hiddenCount(view.state.field(typedReveals)[0]?.segments ?? []);

beforeEach(() => {
	vi.useFakeTimers();
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue(null);
	vi.spyOn(EditorView.prototype, 'visibleRanges', 'get').mockReturnValue([{ from: 0, to: HEAD.length + TAIL.length }]);
	connected.set(true);
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

describe('typing across the connection', () => {
	it('an insert already typing keeps typing through a drop and the reconnect', () => {
		const view = mountEditor();
		agentATypes();
		expect(hidden(view)).toBe(TYPED.length);
		vi.advanceTimersByTime(400);
		const mid = hidden(view);
		expect(mid).toBeGreaterThan(0);
		expect(mid).toBeLessThan(TYPED.length);
		connected.set(false);
		flushSync();
		expect(hidden(view)).toBe(mid);
		vi.advanceTimersByTime(400);
		const during = hidden(view);
		expect(during).toBeGreaterThan(0);
		expect(during).toBeLessThan(mid);
		connected.set(true);
		flushSync();
		vi.advanceTimersByTime(20_000);
		expect(hidden(view)).toBe(0);
	});

	it('an insert arriving while not connected shows at once', () => {
		const view = mountEditor();
		connected.set(false);
		flushSync();
		agentATypes();
		expect(hidden(view)).toBe(0);
	});

	it('a replaced live text settles what was typing', () => {
		const view = mountEditor();
		agentATypes();
		expect(hidden(view)).toBe(TYPED.length);
		const replacement = new Y.Doc().getText('text');
		replacement.insert(0, HEAD + TAIL);
		liveText.set(replacement);
		flushSync();
		expect(hidden(view)).toBe(0);
	});
});
