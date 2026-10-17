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

// Focus is a persisted server setting shared by every spec that runs after it: a spec that turns it off and leaves
// it off (including by failing midway) silently changes what later specs render, and the failure shows up in an
// unrelated spec. Every spec that turns it off must restore it in an afterEach, which runs on failure too.
describe('e2e specs that turn Focus off', () => {
	const turnsOff = /(?:focusSetting|setFocusMode)\([^)]*,\s*false\)/;
	const restoresInAfterEach = /afterEach\([\s\S]*?(?:focusSetting|setFocusMode)\([^)]*,\s*true\)/;

	it('restore it in an afterEach', () => {
		const offenders = specFiles(e2eRoot)
			.filter((file) => {
				const source = readFileSync(file, 'utf8');
				return turnsOff.test(source) && !restoresInAfterEach.test(source);
			})
			.map((file) => path.relative(e2eRoot, file));
		expect(offenders).toEqual([]);
	});
});
