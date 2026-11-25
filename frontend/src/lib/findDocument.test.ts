import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.fn();
vi.mock('$lib/api/request', () => ({ apiFetch: (...args: unknown[]) => apiFetch(...args) }));
import { findDocument } from './findDocument';

beforeEach(() => apiFetch.mockReset());

describe('findDocument', () => {
	it.each([
		['settled registration', { surfacing: 'settled' }],
		['person-owned supervised document', { parent_thread_id: 'person' }],
		['document in a worktree', { path: '/repo/.worktrees/one/doc.md' }],
	])('resolves a %s by artifact id', async (_case, extra) => {
		apiFetch.mockResolvedValue(new Response(JSON.stringify({ artifact: { path: '/w/doc.md', title: 'Doc', ...extra } })));
		const result = await findDocument('artifact/1');
		expect(result).toEqual({ status: 'found', path: 'path' in extra ? extra.path : '/w/doc.md', title: 'Doc' });
		expect(apiFetch).toHaveBeenCalledWith('/api/artifacts/artifact%2F1', { sessionInAddress: true });
	});
	it('opens a document without a path by its external id', async () => {
		apiFetch.mockResolvedValueOnce(new Response(JSON.stringify({ artifact: { path: null, external_id: 'gdrive://doc-1', title: 'Doc' } })));
		expect(await findDocument('no-path')).toEqual({ status: 'found', path: 'gdrive://doc-1', title: 'Doc' });
	});
	it('reports missing for a retired artifact or one with neither a path nor an external id', async () => {
		apiFetch.mockResolvedValueOnce(new Response('', { status: 404 }));
		apiFetch.mockResolvedValueOnce(new Response(JSON.stringify({ artifact: { path: null, external_id: null } })));
		expect(await findDocument('retired')).toEqual({ status: 'missing' });
		expect(await findDocument('no-address')).toEqual({ status: 'missing' });
	});
	it('distinguishes load errors', async () => {
		apiFetch.mockResolvedValueOnce(new Response('', { status: 500 }));
		apiFetch.mockRejectedValueOnce(new Error('offline'));
		expect(await findDocument('id')).toEqual({ status: 'failed' });
		expect(await findDocument('id')).toEqual({ status: 'failed' });
	});
});
