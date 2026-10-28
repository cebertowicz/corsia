import { mount, unmount } from 'svelte';
import { describe, expect, it } from 'vitest';
import ActivityScopeState from './ActivityScopeState.svelte';

describe('Activity scope messages', () => {
	for (const scopeNoun of ['project', 'workspace'] as const) {
		for (const [kind, expected] of [
			['unauthorized', `Your sign-in expired before this ${scopeNoun} loaded.`],
			['forbidden', `Activity isn’t available in this ${scopeNoun}`],
			['not-found', `This ${scopeNoun} isn’t available`],
			['empty', `Plans in this ${scopeNoun} appear here.`],
		] as const) {
			it(`describes ${kind} for a ${scopeNoun}`, () => {
				const host = document.createElement('div');
				const component = mount(ActivityScopeState, {
					target: host,
					props: { kind, scopeNoun, onRetry: () => {}, onGoWorkspace: () => {} },
				});
				expect(host.textContent).toContain(expected);
				if (kind === 'forbidden') expect(host.textContent).toContain(scopeNoun === 'project'
					? 'You don’t have access to this project. Use the sidebar to choose another project or return to Workspace.'
					: 'You don’t have access to this workspace. Use the sidebar to open a project or chat.');
				if (kind === 'not-found') expect(host.textContent).toContain(scopeNoun === 'project'
					? 'Go back to Workspace or choose another project from the sidebar.'
					: 'Use the sidebar to open a project or chat.');
				unmount(component);
			});
		}
	}
});
