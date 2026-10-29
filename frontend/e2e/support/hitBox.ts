import type { Locator } from '@playwright/test';

/** Include an absolutely positioned ::after hit extension, when the control has one. */
export async function hitBox(control: Locator) {
	return control.evaluate((element) => {
		const face = element.getBoundingClientRect();
		const after = getComputedStyle(element, '::after');
		if (after.content === 'none' || after.content === 'normal' || after.position !== 'absolute') return { x: face.x, y: face.y, width: face.width, height: face.height };
		const left = Number.parseFloat(after.left), top = Number.parseFloat(after.top);
		const width = Number.parseFloat(after.width), height = Number.parseFloat(after.height);
		if (![left, top, width, height].every(Number.isFinite)) throw new Error('Cannot measure the control hit extension');
		const borderLeft = Number.parseFloat(getComputedStyle(element).borderLeftWidth) || 0;
		const borderTop = Number.parseFloat(getComputedStyle(element).borderTopWidth) || 0;
		const x = Math.min(face.x, face.x + borderLeft + left), y = Math.min(face.y, face.y + borderTop + top);
		const right = Math.max(face.right, face.x + borderLeft + left + width);
		const bottom = Math.max(face.bottom, face.y + borderTop + top + height);
		return { x, y, width: right - x, height: bottom - y };
	});
}
