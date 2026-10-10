import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import { withLiveUpdateAuthor, type LiveUpdateAuthor } from '$lib/stores/liveUpdateAuthor';
import { liveDocumentBinding } from './live-document-binding';
import { presenceMarks } from './presence-marks';
import { createTypedReveal, typedReveals } from './typed-reveal';
import { hiddenCount } from './typed-reveal-segments';

export const A = { threadId: 'thread-a', name: 'Alpha' };
export const B = { threadId: 'thread-b', name: 'Beta' };
export const TEXT = 'The quick brown fox jumps over the lazy dog. '.repeat(3);

export interface Rig {
	view: EditorView;
	text: Y.Text;
	remote: Y.Text;
	options: { canType: boolean; inView: boolean };
	edit: (author: LiveUpdateAuthor | null, change: (text: Y.Text) => void) => void;
	lanes: () => ReturnType<typeof laneList>;
}
const laneList = (view: EditorView) => view.state.field(typedReveals);

export function rig(initial = 'Start. '): Rig {
	const doc = new Y.Doc();
	const text = doc.getText('t');
	text.insert(0, initial);
	const remoteDoc = new Y.Doc();
	Y.applyUpdate(remoteDoc, Y.encodeStateAsUpdate(doc));
	const remote = remoteDoc.getText('t');
	const options = { canType: true, inView: true };
	const binding = liveDocumentBinding(text);
	const reveal = createTypedReveal({ canType: () => options.canType, isInView: () => options.inView });
	const view = new EditorView({
		state: EditorState.create({ doc: text.toString(), extensions: [binding.extensions, presenceMarks, reveal] }),
		parent: document.body,
	});
	const edit: Rig['edit'] = (author, change) => {
		remoteDoc.transact(() => change(remote));
		const update = Y.encodeStateAsUpdate(remoteDoc, Y.encodeStateVector(doc));
		withLiveUpdateAuthor(author, () => Y.applyUpdate(doc, update, 'server'));
	};
	return { view, text, remote, options, edit, lanes: () => laneList(view) };
}

export const lane = (r: Rig, id: string) => r.lanes().find((l) => l.threadId === id);
export const hiddenOf = (r: Rig, id: string) => hiddenCount(lane(r, id)?.segments ?? []);
export const append = (s: string) => (t: Y.Text) => t.insert(t.length, s);



/**
 * CodeMirror reads `getComputedStyle` on every measure, and jsdom answers it with a full cascade over
 * every stylesheet (several ms each). A typed backlog measures once per frame, so without this memo a
 * test's run time is that cascade times the frame count and stretches with machine load. jsdom has no
 * layout, so an element's computed style changes only with its own class or style attribute or the sheets.
 */
function memoizeComputedStyle(): () => void {
	// The test environment defines window globals as accessors, so install and restore by descriptor.
	const original = Object.getOwnPropertyDescriptor(window, 'getComputedStyle');
	const cascade = window.getComputedStyle;
	const memo = new WeakMap<Element, { key: string; style: CSSStyleDeclaration }>();
	const memoized = (element: Element, pseudo?: string | null) => {
		const key = `${element.getAttribute('class')}|${element.getAttribute('style')}|${document.styleSheets.length}|${pseudo ?? ''}`;
		const hit = memo.get(element);
		if (hit?.key === key) return hit.style;
		const style = cascade.call(window, element, pseudo);
		memo.set(element, { key, style });
		return style;
	};
	Object.defineProperty(window, 'getComputedStyle', { configurable: true, writable: true, value: memoized });
	return () => {
		if (original) Object.defineProperty(window, 'getComputedStyle', original);
		else delete (window as { getComputedStyle?: unknown }).getComputedStyle;
	};
}

export function trackRigs() {
	const rigs: Rig[] = [];
	let restore: (() => void) | null = null;
	return {
		make: (initial?: string) => { restore ??= memoizeComputedStyle(); const r = rig(initial); rigs.push(r); return r; },
		cleanup: () => { rigs.forEach((r) => r.view.destroy()); rigs.length = 0; restore?.(); restore = null; },
	};
}
