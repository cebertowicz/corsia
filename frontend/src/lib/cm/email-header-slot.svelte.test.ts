import { afterEach, describe, expect, it } from 'vitest';
import { flushSync } from 'svelte';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { useEmailHeader, type EmailHeaderOptions } from './email-header-slot.svelte';

Range.prototype.getClientRects ??= () => [] as unknown as DOMRectList;

const DOC = 'From: Alex Rivera <alex@example.com>\nTo: Dana Whitfield <dana@example.com>\nSubject: Saturday\n\nHi Dana,\n';
const OPTIONS: EmailHeaderOptions = { quiet: [], answered: null, header: null, rereads: true, attachmentsKey: '', mountAttachments: null };
let cleanup: (() => void) | null = null;
let view: EditorView | null = null;

afterEach(() => { cleanup?.(); cleanup = null; view?.destroy(); view = null; document.body.replaceChildren(); });

function setup(initial: EmailHeaderOptions | null) {
	let options = $state<EmailHeaderOptions | null>(initial);
	let current = $state<EditorView>();
	cleanup = $effect.root(() => useEmailHeader(() => current, () => options));
	view = new EditorView({ parent: document.body.appendChild(document.createElement('div')), state: EditorState.create({ doc: DOC }) });
	current = view;
	flushSync();
	return { set: (next: EmailHeaderOptions | null) => { options = next; flushSync(); } };
}
const drawn = () => document.querySelector('.cm-email-cc-add') !== null;

describe('the email header slot', () => {
	it('adds the header once the options arrive and removes it when they go', () => {
		const slot = setup(null);
		expect(drawn()).toBe(false);
		slot.set(OPTIONS);
		expect(drawn()).toBe(true);
		slot.set(null);
		expect(drawn()).toBe(false);
		slot.set(OPTIONS);
		expect(drawn()).toBe(true);
	});

	it('draws the header from the start when the options are already there', () => {
		setup(OPTIONS);
		expect(drawn()).toBe(true);
	});

	it('leaves a document that is no email draft alone: no transaction reaches it', () => {
		let dispatched = 0;
		const base = EditorView.prototype.dispatch;
		EditorView.prototype.dispatch = function (this: EditorView, ...args: Parameters<EditorView['dispatch']>) { dispatched += 1; return base.apply(this, args); } as EditorView['dispatch'];
		try { setup(null); } finally { EditorView.prototype.dispatch = base; }
		expect(dispatched).toBe(0);
	});
});
