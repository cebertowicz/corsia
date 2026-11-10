import { expect, it } from 'vitest';
import { roomBeside } from './chatCommentMargin.svelte';

it('shows the column only where the free space right of the centred transcript holds it whole', () => {
	// A 760px transcript and a 320px column: the canvas must leave 320px on each side of the transcript.
	expect(roomBeside(1400, 760, 320)).toBe(true);
	expect(roomBeside(1399, 760, 320)).toBe(false);
	expect(roomBeside(900, 760, 320)).toBe(false);
});

it('never fits when a width cannot be read', () => {
	expect(roomBeside(2000, Number.NaN, 320)).toBe(false);
	expect(roomBeside(2000, 760, Number.NaN)).toBe(false);
	expect(roomBeside(2000, 760, 0)).toBe(false);
});
