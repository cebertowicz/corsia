import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const root = __dirname;
const tokenCss = readFileSync(resolve(root, 'app-tokens.css'), 'utf8');
const safeAreaName = ['safe', 'area', 'inset', 'top'].join('-');
const clearanceConsumers = [
	'lib/components/Header.svelte',
	'lib/components/HomeShell.svelte',
	'lib/components/activity/ActivityView.svelte',
	'lib/components/ArtifactLeaf.svelte',
	'lib/components/MessageList.svelte',
	'lib/components/SettingsView.svelte',
	'lib/components/ProjectSettingsView.svelte',
	'lib/components/ThreadSettingsView.svelte',
	'lib/components/WorkspaceSettingsView.svelte',
	'lib/components/PaneShell.svelte',
	'lib/components/StatusBanners.svelte',
	'lib/components/HostAccessLeaseControl.svelte',
	'lib/components/attention/ChatAttentionStack.svelte',
];

describe('phone header clearance tokens', () => {
	it('defines the header bottom from the one safe-top source', () => {
		expect(tokenCss).toContain(`--safe-top: env(${safeAreaName}, 0px);`);
		expect(tokenCss).toContain('--header-bottom: calc(var(--header-height) + var(--safe-top));');
	});

	it.each(clearanceConsumers)('%s clears the header through --header-bottom', (path) => {
		const source = readFileSync(resolve(root, path), 'utf8');
		expect(source).toContain('var(--header-bottom)');
	});
});
