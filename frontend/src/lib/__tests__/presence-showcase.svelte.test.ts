import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import PresenceShowcase from '../../routes/_design/PresenceShowcase.svelte';

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(async () => {
	if (component) await unmount(component);
	component = null;
	host.remove();
});

describe('/_design document presence showcase', () => {
	it('renders the production cursor, arrival highlight and truthful avatar detail states', () => {
		component = mount(PresenceShowcase, { target: host });
		flushSync();
		expect(host.querySelector('[data-presence-state="cursor"] .cm-presence-cursor')).not.toBeNull();
		expect(host.querySelector('[data-presence-state="arrival"] .cm-presence-arrival')).not.toBeNull();
		expect(host.querySelector('[data-presence-state="arrival"] .participant-trigger.arrival')).not.toBeNull();
		const detailTrigger = host.querySelector<HTMLButtonElement>('[data-participant-id="documents-agent"]');
		expect(detailTrigger?.getAttribute('aria-label')).toBe('Documents');
		expect(host.textContent).toContain('edited 20 s ago');
		expect(host.textContent?.toLowerCase()).not.toContain('now');
	});
});
