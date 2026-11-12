import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { fetchHistory, restoreVersion } from './docHistory';
import { RequestRefused } from '$lib/api/docs';
import { token as session } from '$lib/stores/sessionToken';

const target = { instanceId: 'tab', path: '/w/SKILL.md', name: 'SKILL.md', projectId: 'p', threadId: 't', artifactId: null };
beforeEach(() => session.set('signed-in'));
afterEach(() => { vi.unstubAllGlobals(); session.set(null); });

it('reads the list and whether older versions exist past it', async () => {
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ versions: [], truncated: true }))));
	expect(await fetchHistory(target)).toEqual({ versions: [], truncated: true });
});

it('asks for one restore by version and the revision the person saw, and carries the refusal kind', async () => {
	const fetch = vi.fn(async () => new Response(JSON.stringify({ detail: 'changed', kind: 'stale' }), { status: 409 }));
	vi.stubGlobal('fetch', fetch);
	const refused = await restoreVersion(target, 'v1', 'base').catch((error: unknown) => error);
	expect(refused).toBeInstanceOf(RequestRefused);
	expect((refused as RequestRefused).kind).toBe('stale');
	const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
	expect(url).toBe('/api/files/history/restore?token=signed-in');
	expect((init.headers as Record<string, string>).Authorization).toBe('Bearer signed-in');
	expect(init.method).toBe('POST');
	expect(JSON.parse(String(init.body))).toEqual({ path: '/w/SKILL.md', version: 'v1', base_sha256: 'base', project_id: 'p', thread_id: 't' });
});
