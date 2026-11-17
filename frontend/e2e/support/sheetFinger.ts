import { type Locator, type Page } from '@playwright/test';

/**
 * A finger on a phone sheet, made of touch-type PointerEvents dispatched in the page, so one drag runs the same in Chromium and
 * WebKit (Playwright's WebKit has no touch input but taps). The press goes to the element under the point, as a touch does; each
 * move and the lift go to that same element, as they would under the sheet's pointer capture. Pointer id 1 is the one every
 * browser keeps active, so the sheet's setPointerCapture accepts it rather than throwing for an unknown pointer.
 * Not exercised: the browser's own touch-action handling, which only a real finger drives; `realTouchDrag` stands in for one
 * in Chromium.
 */
async function send(page: Page, type: 'pointerdown' | 'pointermove' | 'pointerup', x: number, y: number): Promise<void> {
	await page.evaluate(({ type, x, y }) => {
		const held = window as unknown as { __sheetFinger?: Element | null };
		if (type === 'pointerdown') held.__sheetFinger = document.elementFromPoint(x, y);
		const target = held.__sheetFinger;
		if (!target) throw new Error(`nothing under the finger at ${Math.round(x)},${Math.round(y)}`);
		target.dispatchEvent(new PointerEvent(type, {
			bubbles: true, cancelable: true, composed: true, pointerId: 1, pointerType: 'touch', isPrimary: true,
			button: type === 'pointermove' ? -1 : 0, buttons: type === 'pointerup' ? 0 : 1, clientX: x, clientY: y,
		}));
		if (type === 'pointerup') held.__sheetFinger = null;
	}, { type, x, y });
}

/** Two frames, so whatever the last step changed has been laid out and painted. */
export const painted = (page: Page) => page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));

/** The middle of what a locator shows. */
export async function centre(target: Locator): Promise<{ x: number; y: number }> {
	const box = await target.boundingBox();
	if (!box) throw new Error('the target is not shown');
	return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
}

/**
 * Presses at `at`, moves by each of `steps` in turn (travel from the press, negative up), reading `each` after every step once it
 * has painted, then lifts; `hold` waits before lifting, so a short pull is not a quick tap. No steps and no hold is a quick tap.
 */
export async function fingerDrag<T>(page: Page, at: { x: number; y: number }, steps: number[], each: () => Promise<T>, hold = 0): Promise<T[]> {
	const seen: T[] = [];
	await send(page, 'pointerdown', at.x, at.y);
	for (const dy of steps) {
		await send(page, 'pointermove', at.x, at.y + dy);
		await painted(page);
		seen.push(await each());
	}
	if (hold) await page.waitForTimeout(hold);
	await send(page, 'pointerup', at.x, at.y + (steps.at(-1) ?? 0));
	return seen;
}

/**
 * The same drag as `fingerDrag`, through Chromium's own touch input (CDP), so the browser decides as it would for a finger
 * whether the touch is the page's to pan or the sheet's to drag (touch-action). Chromium only.
 */
export async function realTouchDrag(page: Page, at: { x: number; y: number }, steps: number[]): Promise<void> {
	const touch = await page.context().newCDPSession(page);
	await touch.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: at.x, y: at.y, id: 1 }] });
	for (const dy of steps) {
		await touch.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: at.x, y: at.y + dy, id: 1 }] });
		await painted(page);
	}
	await touch.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
	await touch.detach();
}
