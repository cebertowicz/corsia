import { describe, expect, it } from 'vitest';
import { keepInPlace, taskSteps } from './activityItems';
import { activityFrameItems } from '$lib/__tests__/fixtures/attentionQueue';

describe('taskSteps', () => {
	it('reads numbered lines as a numbered list, keeping where it starts', () => {
		expect(taskSteps('1. Start the signing.\n2) Enter the code.\n\n3. Press Done.')).toEqual({
			kind: 'numbered', start: 1, items: ['Start the signing.', 'Enter the code.', 'Press Done.'],
		});
		expect(taskSteps('4. Then this.\n5. And this.')).toMatchObject({ kind: 'numbered', start: 4 });
	});

	it('reads bullet lines as a bulleted list', () => {
		expect(taskSteps('- Sign in\n* Enter the code\n• Press Done')).toEqual({
			kind: 'bulleted', items: ['Sign in', 'Enter the code', 'Press Done'],
		});
	});

	it('keeps anything else as written', () => {
		for (const text of ['Sign in and enter the code.', 'Do these:\n1. Sign in\n2. Press Done', '1. Sign in\n- Press Done', '2026 builds ship monthly.']) {
			expect(taskSteps(text)).toEqual({ kind: 'text', text });
		}
	});
});

describe('keepInPlace', () => {
	const all = activityFrameItems();
	const keys = all.map((item) => item.key);
	const without = (key: string) => all.filter((item) => item.key !== key);

	it('puts a kept item the server let go back where the person last saw it', () => {
		const kept = all[5];
		expect(keepInPlace(without(kept.key), kept, keys).map((item) => item.key)).toEqual(keys);
		const first = all[0];
		expect(keepInPlace(without(first.key), first, keys)[0]).toBe(first);
	});

	it('is the server’s list when nothing is kept or the server still returns it', () => {
		const listed = without(all[5].key);
		expect(keepInPlace(listed, null, keys)).toBe(listed);
		expect(keepInPlace(all, all[2], keys)).toBe(all);
	});

	it('puts it last when its place is past the end, or was never seen', () => {
		const kept = all[6];
		const short = all.slice(0, 2);
		expect(keepInPlace(short, kept, keys).at(-1)).toBe(kept);
		expect(keepInPlace(short, kept, []).at(-1)).toBe(kept);
	});
});
