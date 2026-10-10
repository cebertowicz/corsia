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



export function trackRigs() {
	const rigs: Rig[] = [];
	return {
		make: (initial?: string) => { const r = rig(initial); rigs.push(r); return r; },
		cleanup: () => { rigs.forEach((r) => r.view.destroy()); rigs.length = 0; },
	};
}
