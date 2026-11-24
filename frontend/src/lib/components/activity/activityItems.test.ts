import { describe, expect, it } from 'vitest';
import { taskSteps } from './activityItems';

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
