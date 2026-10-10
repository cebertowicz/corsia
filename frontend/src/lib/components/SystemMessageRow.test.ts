import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import SystemMessageRow from './SystemMessageRow.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

function render(msg: unknown): void {
	component = mount(SystemMessageRow, { target: host, props: { msg } });
	flushSync();
}

describe('SystemMessageRow', () => {
	it('puts the expired-Claude recovery line before raw details', () => {
		render({
			id: 'auth-failure', role: 'system',
			content: 'Failed to authenticate: OAuth session expired and could not be refreshed.',
			metadata: { type: 'result', cause: 'sign_in_expired', cc: true },
		});

		expect(host.textContent).toContain("Claude's sign-in has expired. Sign in again from the model card.");
		const details = host.querySelector('details');
		expect(details?.querySelector('summary')?.textContent).toBe('Show details');
		expect(details?.textContent).toContain('Failed to authenticate: OAuth session expired and could not be refreshed.');
	});
});
