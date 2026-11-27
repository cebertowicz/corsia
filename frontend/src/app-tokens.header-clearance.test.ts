import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const root = __dirname;
const tokenCss = readFileSync(resolve(root, 'app-tokens.css'), 'utf8');
const clearanceConsumers = [
	'lib/components/Header.svelte',
	'lib/components/HomeShell.svelte',
	'lib/components/activity/ActivityView.svelte',
	'lib/components/ArtifactLeaf.svelte',
	'lib/components/MessageList.svelte',
	'lib/components/SettingsView.svelte',
	'lib/components/ThreadSettingsView.svelte',
	'lib/components/PaneShell.svelte',
	'lib/components/StatusBanners.svelte',
	'lib/components/HostAccessLeaseControl.svelte',
	'lib/components/attention/ChatAttentionStack.svelte',
];

describe('phone header clearance tokens', () => {
	it('defines the header bottom from the one safe-top source', () => {
		expect(tokenCss).toContain('--safe-top: env(safe-area-inset-top, 0px);');
		expect(tokenCss).toContain('--header-bottom: calc(var(--header-height) + var(--safe-top));');
	});

	it.each(clearanceConsumers)('%s clears the header through --header-bottom', (path) => {
		const source = readFileSync(resolve(root, path), 'utf8');
		expect(source).toContain('var(--header-bottom)');
	});
});

// In the installed iOS app the system blurs the top of the page down to about safe-top + 34px.
// Every surface whose first row meets the top of the screen centres that row at the one shared
// value. It is set only for phones with a top inset; everywhere else (a browser tab, a wider
// window, desktop) it is 0 and each surface keeps its own top.
const topRowSurfaces: [string, string][] = [
	['lib/components/Header.svelte', '--h-row-centre: max(var(--top-row-clearance), var(--top-row-centre));'],
	['lib/components/PaneShell.svelte', 'padding-top: max(var(--safe-top), calc(2 * var(--top-row-centre) - var(--header-bottom)));'],
	['lib/components/Sidebar.svelte', 'height: max(var(--safe-top), calc(var(--top-row-centre) - var(--space-6)));'],
	['lib/components/CommandPalette.svelte', 'padding-top: max(var(--safe-top), calc(var(--top-row-centre) - var(--space-7)));'],
	['lib/components/Lightbox.svelte', 'top: max(var(--safe-top), calc(var(--top-row-centre) - var(--space-7)));'],
	['lib/components/ui/Modal.svelte', '--modal-pad-top:    max(8px, var(--safe-top), calc(var(--top-row-centre) - 31px));'],
	['lib/components/VersionOverlay.svelte', 'min(calc(var(--top-row-centre) * 100), calc(var(--header-bottom) + var(--space-px-2)))'],
];

describe('top-row centre under a top inset', () => {
	it('is safe-top + the clearance on a phone with an inset, and 0 everywhere else', () => {
		expect(tokenCss).toContain('--top-row-clearance: 36px;');
		expect(tokenCss).toContain('--top-row-centre: 0px;');
		const phone = tokenCss.slice(tokenCss.indexOf('@media (max-width: 768px) {'));
		expect(phone).toMatch(/^@media \(max-width: 768px\) \{[^}]*:root \{ --top-row-centre: min\(calc\(var\(--safe-top\) \* 100\), calc\(var\(--safe-top\) \+ var\(--top-row-clearance\)\)\); \}/);
	});

	it.each(topRowSurfaces)('%s places its first row from it', (path, rule) => {
		const source = readFileSync(resolve(root, path), 'utf8');
		expect(source).toContain(rule);
	});
});
