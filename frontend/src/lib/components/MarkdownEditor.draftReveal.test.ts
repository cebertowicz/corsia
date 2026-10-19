import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import { trackOverlaySheet } from '$lib/cm/visible-region';
import MarkdownEditor from './MarkdownEditor.svelte';

const SCREEN = 700;
const rect = (top: number, bottom: number) => ({ top, bottom, height: bottom - top, left: 0, right: 100, width: 100, x: 0, y: top, toJSON: () => ({}) }) as DOMRect;

let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;

/** An editor whose draft text is drawn at `draft` (a box the test moves, as the reader's scroll does) under a sheet the test resizes. */
function draftEditor() {
	host = document.body.appendChild(document.createElement('div'));
	component = mount(MarkdownEditor, { target: host, props: { value: 'Some text to comment on', typingAllowed: true } });
	flushSync();
	const view = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
	view.scrollDOM.style.overflowY = 'auto';
	view.scrollDOM.getBoundingClientRect = () => rect(0, SCREEN);
	const draft = { top: 400, bottom: 420 };
	vi.spyOn(view, 'coordsAtPos').mockImplementation(() => ({ left: 0, right: 10, ...draft }));
	const backdrop = document.body.appendChild(document.createElement('div'));
	backdrop.setAttribute('data-adaptive-drawer', '');
	backdrop.getBoundingClientRect = () => rect(0, SCREEN);
	const panel = backdrop.appendChild(document.createElement('div'));
	let height = 200;
	Object.defineProperty(panel, 'offsetHeight', { get: () => height });
	trackOverlaySheet(host, () => panel);
	const reveal = component as unknown as { draftCovered(from: number, opening: boolean): boolean };
	return { draft, reveal, setSheetHeight(next: number) { height = next; } };
}

afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	document.body.replaceChildren();
	vi.restoreAllMocks();
});

describe('revealing a draft as its sheet moves', () => {
	it('reveals when the sheet comes up', () => {
		const { reveal } = draftEditor();
		expect(reveal.draftCovered(5, true)).toBe(true);
	});

	it('reveals the draft text the sheet or keyboard rises over', () => {
		const { reveal, setSheetHeight } = draftEditor();
		reveal.draftCovered(5, true);
		// The draft is at 400 to 420, above the sheet's top at 500; a keyboard lifts the sheet's top to 350, over the text.
		setSheetHeight(350);
		expect(reveal.draftCovered(5, false)).toBe(true);
	});

	it('leaves a movement that does not reach the text alone', () => {
		const { reveal, setSheetHeight } = draftEditor();
		reveal.draftCovered(5, true);
		setSheetHeight(250);
		expect(reveal.draftCovered(5, false)).toBe(false);
	});

	it('never undoes a scroll the reader chose: text scrolled out of the band stays out as the sheet grows', () => {
		const { reveal, draft, setSheetHeight } = draftEditor();
		reveal.draftCovered(5, true);
		// The reader scrolls the draft's text up out of the band, then the textarea grows the sheet.
		draft.top = -80; draft.bottom = -60;
		setSheetHeight(350);
		expect(reveal.draftCovered(5, false)).toBe(false);
		// Scrolled down to where the sheet now rests: not in view before either.
		draft.top = 600; draft.bottom = 620;
		setSheetHeight(450);
		expect(reveal.draftCovered(5, false)).toBe(false);
	});
});
