import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// jsdom does no layout and applies no scoped CSS, so the widths are worked out from the rules and tokens the components ship.
const read = (path: string) => readFileSync(resolve(process.cwd(), path), 'utf8');
const field = read('src/lib/components/comments/ReplyField.svelte');
const button = read('src/lib/components/ui/Button.svelte');
const tokens = read('src/app-tokens.css');

const BORDER = 1;
const padX = Number(tokens.match(/--button-pad-x-sm: (\d+)px/)?.[1]);
const iconWidth = Number(field.match(/<svg width="(\d+)"/)?.[1]);
const spinnerWidth = Number(button.match(/\.spinner \{[^}]*?width: (\d+)px/)?.[1]);
const slotRule = field.match(/\.ct-send-slot \{[^}]*\}/)?.[0] ?? '';
const slotMinWidth = slotRule.match(/min-width: calc\((\d+)px \+ 2 \* var\(--button-pad-x-sm\) \+ (\d+)px\)/);

describe('Reply Send width while a reply posts', () => {
	it('reads the numbers it checks', () => {
		for (const value of [padX, iconWidth, spinnerWidth]) expect(value).toBeGreaterThan(0);
		expect(slotMinWidth).not.toBeNull();
	});

	it('holds the slot at the width Send has with its arrow, so the field beside it does not move', () => {
		const idle = iconWidth + 2 * padX + 2 * BORDER;
		const slot = Number(slotMinWidth![1]) + 2 * padX + Number(slotMinWidth![2]);
		expect(slot).toBe(idle);
	});

	it('is never narrower than Send with its spinner, so the slot stays one width in both states', () => {
		const posting = spinnerWidth + 2 * padX + 2 * BORDER;
		expect(Number(slotMinWidth![1]) + 2 * padX + Number(slotMinWidth![2])).toBeGreaterThanOrEqual(posting);
	});
});
