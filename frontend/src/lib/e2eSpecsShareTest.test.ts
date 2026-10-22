import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const e2eDir = path.resolve(__dirname, '../../e2e');

function specs(dir: string): string[] {
	return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
		const full = path.join(dir, entry.name);
		if (entry.isDirectory()) return specs(full);
		return entry.name.endsWith('.spec.ts') ? [full] : [];
	});
}

/** The `{ ... }` names a spec imports from one module, or null when it does not import from it. */
function importedNames(source: string, matchModule: (module: string) => boolean): string[] | null {
	const found = [...source.matchAll(/import\s*\{([^}]*)\}\s*from\s*['"]([^'"]+)['"]/g)].filter(([, , module]) => matchModule(module));
	if (!found.length) return null;
	return found.flatMap(([, names]) => names.split(',').map((name) => name.trim().split(/\s+as\s+/)[0]).filter(Boolean));
}

describe('every e2e spec takes its test from support/test', () => {
	// The suite resets the backend to its seed in an auto fixture on `test` (support/test.ts), so a spec using Playwright's
	// own `test` runs on whatever the previous file left behind and passes or fails by file order.
	const files = specs(e2eDir);

	it('finds the specs', () => {
		expect(files.length).toBeGreaterThan(50);
	});

	it.each(files.map((file) => [path.relative(e2eDir, file), file]))('%s', (_name, file) => {
		const source = readFileSync(file, 'utf8');
		expect(importedNames(source, (module) => /(^|\/)support\/test$/.test(module)), 'imports test from support/test').toContain('test');
		expect(importedNames(source, (module) => module === '@playwright/test') ?? [], 'does not import test from @playwright/test').not.toContain('test');
	});
});
