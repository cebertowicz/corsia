import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ConnectedAppsSettings from './ConnectedAppsSettings.svelte';
import { requestConnectedApp, resetSettingsNavigation } from '$lib/stores/settingsNavigation';
import { token } from '$lib/stores/auth';
import { emit } from '$lib/ws/events';

const app = { id: 'a', name: 'App A', state: 'awaiting_approval', revision: 1, reach: { mode: 'all', project_ids: [], outside_projects: false } };
const account = { id: 'account', provider: 'google', provider_display_name: 'Google', address: 'sam@example.com', capabilities: ['mail'], state: 'connected', tenant_id: null, reach: { mode: 'corsia', project_ids: [], outside_projects: false } };
let host: HTMLElement;
let component: Record<string, unknown>;

beforeEach(() => {
 resetSettingsNavigation();
 token.set('test-token');
 vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ measureText: (text: string) => ({ width: text.length * 8 }) } as CanvasRenderingContext2D);
 HTMLElement.prototype.scrollIntoView = vi.fn();
 vi.stubGlobal('CSS', { escape: (value: string) => value });
 host = document.createElement('div');
 document.body.appendChild(host);
});
afterEach(() => { unmount(component); host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); token.set(null); });

it('handles each app link once across refreshes, including repeated and different links', async () => {
 const b = { ...app, id: 'b', name: 'App B' };
 let refreshed = false;
 const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ servers: [app, { ...b, notice: refreshed ? 'Updated' : null }], accounts: [account] }) }));
 vi.stubGlobal('fetch', fetch);
 component = mount(ConnectedAppsSettings, { target: host });
 await vi.waitFor(() => expect(host.querySelectorAll('[data-connected-app-id]')).toHaveLength(2));
 const approve = (id: string) => host.querySelector<HTMLButtonElement>(`[data-connected-app-id="${id}"] .row-action button`)!;
 requestConnectedApp('a');
 await vi.waitFor(() => expect(document.activeElement).toBe(approve('a')));
 approve('b').focus();
 refreshed = true;
 emit('connected_apps_updated', {});
 await vi.waitFor(() => expect(host.textContent).toContain('Updated'));
 await new Promise(requestAnimationFrame);
 expect(document.activeElement).toBe(approve('b'));
 requestConnectedApp('a');
 await vi.waitFor(() => expect(document.activeElement).toBe(approve('a')));
 requestConnectedApp('b');
 await vi.waitFor(() => expect(document.activeElement).toBe(approve('b')));
});

it('does not reopen a linked account or steal focus on refresh', async () => {
 let refreshed = false;
 const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ servers: [{ ...app, notice: refreshed ? 'Updated' : null }], accounts: [account] }) }));
 vi.stubGlobal('fetch', fetch);
 component = mount(ConnectedAppsSettings, { target: host });
 await vi.waitFor(() => expect(host.querySelector('[data-connected-account-id="account"]')).not.toBeNull());
 requestConnectedApp('account');
 const row = () => host.querySelector<HTMLElement>('[data-connected-account-id="account"]')!;
 await vi.waitFor(() => expect(document.activeElement?.textContent?.trim()).toBe('Where it works…'));
 row().querySelector<HTMLButtonElement>('.face.open')!.click();
 flushSync();
 const other = host.querySelector<HTMLButtonElement>('[data-connected-app-id="a"] button')!;
 other.focus();
 refreshed = true;
 emit('connected_apps_updated', {});
 await vi.waitFor(() => expect(host.textContent).toContain('Updated'));
 await new Promise(requestAnimationFrame);
 expect(row().querySelector('.face.open')?.getAttribute('aria-expanded')).toBe('false');
 expect(document.activeElement).toBe(other);
});
