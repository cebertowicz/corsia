import { readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const e2eRoot = path.resolve(__dirname, '../../e2e');

function specFiles(dir: string): string[] {
	return readdirSync(dir).flatMap((name) => {
		const full = path.join(dir, name);
		if (statSync(full).isDirectory()) return name === 'support' ? [] : specFiles(full);
		return name.endsWith('.spec.ts') ? [full] : [];
	});
}

/** The text between the parentheses of the call whose opening parenthesis is at `open`. */
function callArguments(source: string, open: number): string {
	let depth = 0;
	for (let i = open; i < source.length; i++) {
		if (source[i] === '(') depth++;
		if (source[i] === ')' && --depth === 0) return source.slice(open + 1, i);
	}
	return source.slice(open + 1);
}

const focusCalls = /\b(?:focusSetting|setFocusMode)\(\s*\w+\s*,\s*([^)]*?)\s*\)/g;
const restoreCall = /\bsetFocusMode\(\s*request\s*,\s*true\s*\)/;

/** True when the spec turns Focus off, or sets it to anything that is not the literal `true`. */
function turnsFocusOff(source: string): boolean {
	return [...source.matchAll(focusCalls)].some((call) => call[1] !== 'true');
}

/** True when an afterEach callback itself restores Focus through the API, which needs no page state. */
function restoresInAfterEach(source: string): boolean {
	return [...source.matchAll(/\bafterEach\(/g)].some((hook) => restoreCall.test(callArguments(source, hook.index + hook[0].length - 1)));
}

// Focus is a persisted server setting shared by every spec that runs after it: a spec that turns it off and leaves
// it off (including by failing midway) silently changes what later specs render, and the failure shows up in an
// unrelated spec. Every spec that turns it off must restore it with `setFocusMode(request, true)` inside an
// afterEach, which runs on failure too and, unlike a Settings-UI restore, does not depend on where the page ended.
describe('e2e specs that turn Focus off', () => {
	it('restore it through the API in an afterEach', () => {
		const offenders = specFiles(e2eRoot)
			.filter((file) => {
				const source = readFileSync(file, 'utf8');
				return turnsFocusOff(source) && !restoresInAfterEach(source);
			})
			.map((file) => path.relative(e2eRoot, file));
		expect(offenders).toEqual([]);
	});

	it('is not satisfied by a restore outside the afterEach callback', () => {
		const lateRestore = [
			"test.afterEach(async ({ page }) => { await page.goto('/'); });",
			"test('x', async ({ request }) => { await setFocusMode(request, false); await setFocusMode(request, true); });",
		].join('\n');
		expect(turnsFocusOff(lateRestore)).toBe(true);
		expect(restoresInAfterEach(lateRestore)).toBe(false);
		expect(turnsFocusOff("await setFocusMode(request, enabled);")).toBe(true);
		expect(restoresInAfterEach('test.afterEach(async ({ request }) => {\n await setFocusMode(request, true);\n});')).toBe(true);
	});
});
