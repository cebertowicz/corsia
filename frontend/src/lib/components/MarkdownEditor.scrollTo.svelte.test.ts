import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import { trackOverlaySheet } from '$lib/cm/visible-region';
import type { ScrollOrigin } from '$lib/cm/comment-marks';
import MarkdownEditor from './MarkdownEditor.svelte';

let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;

afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	document.body.replaceChildren();
	vi.restoreAllMocks();
});

const jumpTo = (from: number) => ({ from, to: from + 4, focus: true, origin: 'navigation' as ScrollOrigin });

describe('jumping to a scroll target', () => {
	it('focuses the text once per target, not again when the sheet over the text closes', () => {
		host = document.body.appendChild(document.createElement('div'));
		// The comment list sheet is up over the text; the jump measures the band it leaves visible.
		let sheetUp = $state(true);
		const panel = document.body.appendChild(document.createElement('div'));
		trackOverlaySheet(host, () => (sheetUp ? panel : null));
		const props = $state({ value: 'Some text to comment on', scrollTo: jumpTo(0) });
		const focus = vi.spyOn(EditorView.prototype, 'focus');
		component = mount(MarkdownEditor, { target: host, props });
		flushSync();
		props.scrollTo = jumpTo(5);
		flushSync();
		expect(focus).toHaveBeenCalledTimes(2);
		// Closing the sheet hands focus back to its opener; the jump that already landed does not replay and take it.
		sheetUp = false;
		flushSync();
		expect(focus).toHaveBeenCalledTimes(2);
	});
});
