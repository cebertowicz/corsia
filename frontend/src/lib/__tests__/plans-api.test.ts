import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
	readActivityWorkspace,
	readActivityProject,
	readOperatorWorkspace,
	readOperatorProject,
	readOperatorThread,
	readOperatorChild,
	mutatePlanLifecycle,
} from '$lib/api/plans';
import type { ActivityIndexEnvelope, OperatorEnvelope } from '$lib/api/plans';
import { errorText } from '$lib/api/planRead';

const TOKEN = '[REDACTED]';

const ACTIVITY_ENVELOPE: ActivityIndexEnvelope = {
	contract_version: 1,
	representation: 'activity_scope_index',
	as_of: '2026-08-01T00:00:00+00:00',
	target: { kind: 'workspace_scope', id: 'workspace', label: 'Workspace' },
	revision_token: 'activity-r1',
	plans: [],
	attention: { awaiting_count: 0 },
};

const ENVELOPE: OperatorEnvelope = {
	contract_version: 2,
	as_of: '2026-08-01T00:00:00+00:00',
	target: { kind: 'thread', id: 'thread-1', label: 'Thread' },
	revision_token: '[REDACTED]',
	plans: [],
	attention: { awaiting_count: 0 },
};

function mockFetch(status: number, body: unknown, headers: Record<string, string> = {}) {
	const response = {
		ok: status >= 200 && status < 300,
		status,
		json: () => Promise.resolve(body),
		text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
		headers: { get: (key: string) => headers[key] ?? null },
	};
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response));
}

beforeEach(() => vi.unstubAllGlobals());
afterEach(() => vi.restoreAllMocks());

describe('plan lifecycle mutations', () => {
	it('posts a closed pause operation with revision and idempotency evidence', async () => {
		mockFetch(200, { plan: { id: 'plan-1', state: 'paused', revision: 2 } });
		const result = await mutatePlanLifecycle(
			TOKEN, 'plan/1', 'pause', 1, 'pause:plan-1:1',
		);
		expect(result).toEqual({ status: 'success' });
		const [url, opts] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
		expect(url).toBe('/api/plans/plan%2F1/mutations');
		expect(opts.method).toBe('POST');
		expect(opts.body).toBe(JSON.stringify({
			operation: 'pause',
			mutation_id: 'pause:plan-1:1',
			params: { expected_revision: 1 },
		}));
		expect((opts.headers as Record<string, string>).Authorization).toBe(`Bearer ${TOKEN}`);
	});

	it('classifies stale, forbidden, and server failures without exposing bodies', async () => {
		mockFetch(409, { detail: 'internal revision data' });
		expect(await mutatePlanLifecycle(TOKEN, 'p', 'resume', 2, 'resume:p:2')).toEqual({
			status: 'conflict',
		});
		mockFetch(403, { detail: 'internal principal' });
		expect(await mutatePlanLifecycle(TOKEN, 'p', 'resume', 2, 'resume:p:2')).toEqual({
			status: 'forbidden',
		});
		mockFetch(500, { detail: '/secret/path.py' });
		expect(await mutatePlanLifecycle(TOKEN, 'p', 'resume', 2, 'resume:p:2')).toEqual({
			status: 'error',
			message: 'Couldn’t update the plan. Try again.',
		});
	});

	it('keeps a server failure\'s log reference beside the message, never its detail', async () => {
		mockFetch(500, { detail: '/secret/path.py', reference: 'abc123' });
		const result = await mutatePlanLifecycle(TOKEN, 'p', 'resume', 2, 'resume:p:2');
		expect(result).toEqual({ status: 'error', message: 'Couldn’t update the plan. Try again.', reference: 'abc123' });
		expect(errorText(result as { message: string; reference?: string })).toBe('Couldn’t update the plan. Try again. Reference: abc123');
		expect(JSON.stringify(result)).not.toContain('secret');

		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
			ok: false, status: 502, json: () => Promise.reject(new SyntaxError('Unexpected token <')),
			headers: { get: () => null },
		}));
		expect(await mutatePlanLifecycle(TOKEN, 'p', 'resume', 2, 'resume:p:2')).toEqual({
			status: 'error', message: 'Couldn’t update the plan. Try again.',
		});
	});
});

describe('compact Activity reads', () => {
	it('uses dedicated workspace/project routes and its own decoder', async () => {
		mockFetch(200, ACTIVITY_ENVELOPE, { ETag: '"activity-1"' });
		expect(await readActivityWorkspace(TOKEN)).toEqual({
			status: 'fresh', payload: ACTIVITY_ENVELOPE, etag: '"activity-1"',
		});
		expect(vi.mocked(fetch).mock.calls[0][0]).toBe('/api/plans/activity/scopes/workspace');

		mockFetch(200, ACTIVITY_ENVELOPE, { ETag: '"activity-2"' });
		await readActivityProject(TOKEN, 'project/weird id', '"activity-1"');
		const [url, opts] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
		expect(url).toBe('/api/plans/activity/scopes/projects/project%2Fweird%20id');
		expect((opts.headers as Record<string, string>)['If-None-Match']).toBe('"activity-1"');
	});

	it('rejects the full operator representation on the compact route', async () => {
		mockFetch(200, ENVELOPE, { ETag: '"operator"' });
		expect(await readActivityWorkspace(TOKEN)).toEqual({
			status: 'error', message: 'Couldn’t read this plan. Reload the page.',
		});
	});
});

describe('contextual operator reads', () => {
	it('returns a fresh thread payload with ETag and authorization', async () => {
		mockFetch(200, ENVELOPE, { ETag: '"rev-1"' });
		const result = await readOperatorThread(TOKEN, 'thread-1');
		expect(result).toEqual({ status: 'fresh', payload: ENVELOPE, etag: '"rev-1"' });
		const [url, opts] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
		expect(url).toBe('/api/plans/operator/threads/thread-1');
		expect((opts.headers as Record<string, string>).Authorization).toBe(`Bearer ${TOKEN}`);
		expect((opts.headers as Record<string, string>)['If-None-Match']).toBeUndefined();
	});

	it('sends If-None-Match and maps 304 without a payload', async () => {
		mockFetch(304, null);
		expect(await readOperatorThread(TOKEN, 'thread-1', '"rev-1"')).toEqual({
			status: 'not-modified',
		});
		const [, opts] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
		expect((opts.headers as Record<string, string>)['If-None-Match']).toBe('"rev-1"');
	});

	it('maps unavailable targets without disclosing response bodies', async () => {
		mockFetch(404, { detail: 'nope' });
		expect(await readOperatorThread(TOKEN, 'thread-1')).toEqual({ status: 'not-found' });
		mockFetch(403, { detail: 'nope' });
		expect(await readOperatorThread(TOKEN, 'thread-1')).toEqual({ status: 'forbidden' });
	});

	it('a 2xx reply that is not JSON is a failed read, not a hang', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
			ok: true, status: 200, json: () => Promise.reject(new SyntaxError('Unexpected token')),
			headers: { get: () => null },
		}));
		expect(await readOperatorThread(TOKEN, 'thread-1')).toEqual({ status: 'error', message: 'Couldn’t read this plan. Reload the page.' });
	});

	it('a 2xx body that is not the envelope is a failed read, not fresh state', async () => {
		mockFetch(200, { plans: 'none' }, { ETag: '"x"' });
		expect(await readOperatorThread(TOKEN, 'thread-1')).toEqual({ status: 'error', message: 'Couldn’t read this plan. Reload the page.' });
	});

	it('401 is unauthorized, not a retryable error', async () => {
		mockFetch(401, { detail: 'Not authenticated' });
		expect(await readOperatorThread(TOKEN, 'thread-1')).toEqual({ status: 'unauthorized' });
	});

	it('normalizes network and server failures', async () => {
		vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
		expect(await readOperatorThread(TOKEN, 'thread-1')).toEqual({
			status: 'error',
			message: 'Couldn’t load the plan. Try again.',
		});

		mockFetch(500, { detail: 'internal/secret/path.py:42' });
		const result = await readOperatorThread(TOKEN, 'thread-1');
		expect(result).toEqual({
			status: 'error',
			message: 'Couldn’t load the plan. Try again.',
		});

		mockFetch(500, { detail: 'internal/secret/path.py:42', reference: 'def456' });
		expect(await readOperatorThread(TOKEN, 'thread-1')).toEqual({
			status: 'error', message: 'Couldn’t load the plan. Try again.', reference: 'def456',
		});
	});

	it('hits and URL-encodes the scope, thread, and child routes', async () => {
		mockFetch(200, ENVELOPE, { ETag: '"r"' });
		await readOperatorWorkspace(TOKEN);
		expect(vi.mocked(fetch).mock.calls[0][0]).toBe(
			'/api/plans/operator/scopes/workspace',
		);

		mockFetch(200, ENVELOPE, { ETag: '"r"' });
		await readOperatorProject(TOKEN, 'project/weird id');
		expect(vi.mocked(fetch).mock.calls[0][0]).toBe(
			'/api/plans/operator/scopes/projects/project%2Fweird%20id',
		);

		mockFetch(200, ENVELOPE, { ETag: '"r"' });
		await readOperatorThread(TOKEN, 'thread/weird id');
		expect(vi.mocked(fetch).mock.calls[0][0]).toBe(
			'/api/plans/operator/threads/thread%2Fweird%20id',
		);

		mockFetch(200, ENVELOPE, { ETag: '"r"' });
		await readOperatorChild(TOKEN, 'node/weird id');
		expect(vi.mocked(fetch).mock.calls[0][0]).toBe(
			'/api/plans/operator/nodes/node%2Fweird%20id/child',
		);
	});
});
