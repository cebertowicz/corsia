/**
 * A click on a scrim dismisses only a press that began on the scrim itself. The click that follows the tap which opened
 * a sheet lands on the new scrim with no press of its own on it, so it is not a tap outside. Wire `down` to the scrim's
 * pointerdown and `click` to its click; `dismiss` decides whether this surface closes from its scrim at all.
 */
export function scrimPress(dismiss: () => void) {
	let pressed = false;
	return {
		down(event: PointerEvent): void {
			pressed = event.target === event.currentTarget;
		},
		click(event: MouseEvent): void {
			const began = pressed;
			pressed = false;
			if (began && event.target === event.currentTarget) dismiss();
		},
	};
}
