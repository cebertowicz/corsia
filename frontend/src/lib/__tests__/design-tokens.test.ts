import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

/**
 * A reference to an undefined custom property is silent: the browser drops the
 * whole declaration, and — inside `color-mix()` — invalidates the entire
 * function. That is how a drawer and two sticky bars shipped with no background
 * at all while looking correct in code review.
 *
 * This guard reads the authored sources directly rather than a built bundle, so
 * it fails on the line a contributor wrote.
 */

const SRC = resolve(__dirname, '../..');

/** Properties written at runtime with `style.setProperty`, never authored in CSS. */
const RUNTIME_PROVIDED = new Set([
	'--kb-toolbar-inset',
	'--empty-parallax-offset',
	'--messages-pad-top',
	'--chat-attention-occupied-height',
	'--floater-bottom-pad',
	'--input-bar-height',
	// Set per-instance via Svelte `style:--x` or a parent inline style.
	'--drawer-drag-y',
	'--depth',
	// The comment rail width: DocViewer's `style:--doc-rail` and the editor's rail, both from railWidth().
	'--doc-rail',
	// One image comment's author colour: each tag, box, dot and card number sets `style:--region-color` from regionColor().
	'--region-color',
	// ToastContainer's `style:--toast-clearance` (the toastClearance store) and `style:--toast-height`.
	'--toast-clearance',
	'--toast-height',
]);

/**
 * Undefined references that already existed before this guard, each dropping a
 * real declaration today. Choosing the right replacement needs the judgment of
 * whoever owns each surface, so they are recorded rather than guessed at.
 *
 * This list may only shrink. A new entry means a new silently-dropped
 * declaration and should be fixed instead of appended.
 */
const KNOWN_UNRESOLVED_BASELINE_COUNT = 7;
const KNOWN_UNRESOLVED = new Set([
	'lib/components/ThreadAgentPanel.svelte:356 → var(--text-quaternary)',
	'lib/components/InputBar.css:133 → var(--amber-2)',
	'lib/components/InputBar.css:143 → var(--sky-2)',
	'lib/components/v7/OmlxServerInfoPanel.svelte:137 → var(--surface-2)',
	'lib/components/v7/OmlxServerInfoPanel.svelte:143 → var(--link)',
	'lib/components/v7/OmlxServerInfoPanel.svelte:150 → var(--err)',
	'lib/components/v7/OmlxServerInfoPanel.svelte:190 → var(--surface-2)',
]);

function walk(dir: string, out: string[] = []): string[] {
	for (const entry of readdirSync(dir)) {
		if (entry === 'node_modules' || entry.startsWith('.')) continue;
		const path = join(dir, entry);
		if (statSync(path).isDirectory()) walk(path, out);
		else if (/\.(svelte|css)$/.test(entry)) out.push(path);
	}
	return out;
}

const files = walk(SRC);

/** Every `--x:` definition anywhere in the authored sources. */
const defined = new Set<string>(RUNTIME_PROVIDED);
for (const file of files) {
	const text = readFileSync(file, 'utf8');
	for (const match of text.matchAll(/(--[a-zA-Z0-9-]+)\s*:/g)) defined.add(match[1]);
}

interface Reference {
	token: string;
	file: string;
	line: number;
}

const references: Reference[] = [];
for (const file of files) {
	const lines = readFileSync(file, 'utf8').split('\n');
	lines.forEach((text, index) => {
		for (const match of text.matchAll(/var\(\s*(--[a-zA-Z0-9-]+)/g)) {
			references.push({ token: match[1], file: file.slice(SRC.length + 1), line: index + 1 });
		}
	});
}

describe('design tokens', () => {
	it('finds token definitions and references to check', () => {
		// Guards the guard: a broken walk or regex would otherwise pass vacuously.
		expect(defined.size).toBeGreaterThan(100);
		expect(references.length).toBeGreaterThan(500);
	});

	it('never references a custom property that is defined nowhere', () => {
		const unresolved = new Set(references
			.filter((reference) => !defined.has(reference.token))
			.map((reference) => `${reference.file}:${reference.line} → var(${reference.token})`));
		expect([...unresolved].filter((entry) => !KNOWN_UNRESOLVED.has(entry))).toEqual([]);
	});

	it('keeps the known-unresolved baseline shrink-only', () => {
		// Fixes may remove entries. Appending a tenth silently-dropped declaration
		// fails until the new reference itself is fixed.
		expect(KNOWN_UNRESOLVED.size).toBeLessThanOrEqual(KNOWN_UNRESOLVED_BASELINE_COUNT);
	});

	it('keeps no stale entry in the known-unresolved list', () => {
		const unresolved = new Set(references
			.filter((reference) => !defined.has(reference.token))
			.map((reference) => `${reference.file}:${reference.line} → var(${reference.token})`));
		// A fixed or moved line must be removed from the list, so the list cannot
		// quietly grant amnesty to a different defect at the same coordinates.
		expect([...KNOWN_UNRESOLVED].filter((entry) => !unresolved.has(entry))).toEqual([]);
	});

	it('resolves the material tokens the drawer family depends on', () => {
		// The exact class of bug this guard exists for: a drawer, two sticky bars
		// and an error box whose background silently evaluated to nothing.
		for (const token of ['--glass-thick', '--glass-blur', '--bg-elevated', '--glass-scrim']) {
			expect(defined.has(token), `${token} must be defined`).toBe(true);
		}
		expect(defined.has('--surface-1')).toBe(false);
		expect(references.some((reference) => reference.token === '--surface-1')).toBe(false);
	});

	it('registers the theme-independent image-region contrast tokens', () => {
		for (const token of ['--image-region-ink', '--image-region-halo', '--image-region-edge']) {
			expect(defined.has(token), `${token} must be defined`).toBe(true);
		}
	});
});
