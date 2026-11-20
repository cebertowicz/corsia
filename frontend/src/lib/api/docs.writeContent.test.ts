import { afterEach, describe, expect, it, vi } from 'vitest';

const requests = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock('./request', async (importOriginal) => ({ ...(await importOriginal<typeof import('./request')>()), apiFetch: requests.apiFetch }));

import { writeContent } from './docs';
import { saveErrorCode, saveErrorMessage } from '$lib/stores/docSaveError';

afterEach(() => requests.apiFetch.mockReset());

const sentBody = () => JSON.parse(requests.apiFetch.mock.calls[0][1].body);

describe('writeContent', () => {
	it('sends the base digest as expected_sha256', async () => {
		requests.apiFetch.mockResolvedValue(new Response('{"saved":true}'));
		await writeContent('/p/a.md', 'text', { projectId: 'p', threadId: 't', expectedSha256: 'abc123' });
		expect(sentBody()).toMatchObject({ path: '/p/a.md', text: 'text', project_id: 'p', thread_id: 't', expected_sha256: 'abc123' });
	});

	it('sends null when there is no digest, which the server treats as the plain overwrite', async () => {
		requests.apiFetch.mockResolvedValue(new Response('{"saved":true}'));
		await writeContent('/p/a.md', 'text');
		expect(sentBody().expected_sha256).toBeNull();
	});
});

describe('the editor stale refusal as the tab reads it', () => {
	const refusal = new Error(`409 ${JSON.stringify({ detail: { code: 'stale_base', message: 'Placeholder wording.' } })}`);

	it('exposes its stable code and keeps its message', () => {
		expect(saveErrorCode(refusal)).toBe('stale_base');
		expect(saveErrorMessage(refusal)).toBe('Placeholder wording.');
	});

	it.each([
		['a string detail', new Error('409 {"detail":"Moved."}')],
		['a non-JSON failure', new Error('502 <html>bad gateway</html>')],
		['a detail with no code', new Error('409 {"detail":{"message":"Nope"}}')],
		['something that is not an Error', 'boom'],
	])('names no code for %s', (_label, error) => {
		expect(saveErrorCode(error)).toBeNull();
	});
});
