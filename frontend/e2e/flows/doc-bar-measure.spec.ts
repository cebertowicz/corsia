import { test, expect } from '../support/test';
import { expectTouchTargets, paintedBoxes } from '../support/documentBar';

/**
 * The clearance helper measures what a control paints. Text a person cannot see is not painted: a screen-reader sentence
 * hidden with `clip: rect(0,0,0,0)` (every `.sr-only`, as in AvatarStack) or `clip-path: inset(100%)` (the Popover primitive's
 * closed state) sits beside its button without taking the room next to it. A label that really runs past its box still does.
 */
const SCREEN_READER_ONLY = 'position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0';
const CLIPPED_AWAY = 'position:absolute;left:0;top:0;white-space:nowrap;clip-path:inset(100%)';
const SENTENCE = 'Reader 0, who read this document a minute ago, and has not changed it since';

test('@flow:doc-bar-measure screen-reader text beside a button is not painted room, and a label past its box still is', async ({ page }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'The helper is measured once, in one browser');
	const button = (id: string, inner: string, style = '') => `<button id="${id}" style="position:relative;width:40px;height:40px;padding:0;${style}">${inner}</button>`;
	await page.setContent(`<div id="bar" style="display:flex;gap:4px;padding:8px;width:900px;position:relative">
		${button('sr', `S<span style="${SCREEN_READER_ONLY}">${SENTENCE}</span>`)}
		${button('after-sr', 'B')}
		${button('away', `C<span style="${CLIPPED_AWAY}">${SENTENCE}</span>`)}
		${button('after-away', 'D')}
		${button('label', 'A label that really does run past its own box', 'white-space:nowrap;overflow:visible')}
		${button('after-label', 'F')}
	</div>`);
	const bar = page.locator('#bar');
	const [sr, away, label] = [await paintedBoxes(bar, '#sr'), await paintedBoxes(bar, '#away'), await paintedBoxes(bar, '#label')].map((boxes) => boxes[0]);
	// Hidden sentences add nothing: what each paints is what its box is.
	for (const control of [sr, away]) {
		expect(control.ink.right, `${control.name} paints no further than its box: ${JSON.stringify(control)}`).toBeLessThanOrEqual(control.right + 0.5);
		expect(control.ink.left, `${control.name} paints no further than its box: ${JSON.stringify(control)}`).toBeGreaterThanOrEqual(control.left - 0.5);
	}
	// So the 4px between each of them and the next button is room, and the check says so.
	await expectTouchTargets(bar, '#sr, #away', { min: 40, gap: 4 });
	// A visible label wider than its box does reach the next control: the helper still sees it.
	expect(label.ink.right, `a visible label past its box is painted: ${JSON.stringify(label)}`).toBeGreaterThan(label.right + 20);
});
