import { createHash } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';

// The real writeContent runs here: these tests read the PUT body the queue sends, not the argument it hands the client.
const requests = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock('$lib/api/request', async (importOriginal) => ({ ...(await importOriginal<typeof import('$lib/api/request')>()), apiFetch: requests.apiFetch }));
vi.mock('$lib/stores/docComments', () => ({ flushPositions: vi.fn() }));
vi.mock('$lib/stores/toast', () => ({ toasts: { show: vi.fn() } }));

import { clearDraft, flushDraft, seedDraft, setDraftText } from './docSaveQueue';
import type { DocViewerTarget } from '$lib/docViewerTarget';

const sha = (text: string) => createHash('sha256').update(text, 'utf8').digest('hex');
let seq = 0;
const target = (): DocViewerTarget => ({ instanceId: `request-tab-${++seq}`, path: '/project/note.md', name: 'note.md', projectId: 'project-a', threadId: 'thread-a', artifactId: 'artifact-a' });
const putBody = () => {
	const [url, init] = requests.apiFetch.mock.calls.at(-1)!;
	expect(url).toBe('/api/files/content');
	expect(init.method).toBe('PUT');
	return JSON.parse(init.body);
};

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
	requests.apiFetch.mockReset();
});

describe('the PUT the save queue sends', () => {
	it.each([
		['with WebCrypto', false],
		['by software when the browser has no WebCrypto', true],
	])('carries the digest of the base text %s', async (_label, withoutSubtle) => {
		requests.apiFetch.mockImplementation(async () => new Response('{"saved":true}'));
		if (withoutSubtle) vi.stubGlobal('crypto', {});
		const doc = target(), base = 'Über café 漢字 🙂\r\n';
		seedDraft(doc, base);
		setDraftText(doc, 'typed');
		expect(await flushDraft(doc)).toBe(true);
		expect(putBody()).toMatchObject({ path: '/project/note.md', text: 'typed', expected_sha256: sha(base) });
		clearDraft(doc);
	});

	it('carries expected_sha256 null, the old overwrite, for a base that holds U+FFFD', async () => {
		requests.apiFetch.mockImplementation(async () => new Response('{"saved":true}'));
		const doc = target();
		seedDraft(doc, 'caf�');
		setDraftText(doc, 'cafe');
		expect(await flushDraft(doc)).toBe(true);
		expect(requests.apiFetch).toHaveBeenCalledOnce();
		expect(putBody()).toMatchObject({ text: 'cafe', expected_sha256: null });
		clearDraft(doc);
	});

	it('builds the next save on the text the last one wrote, so a U+FFFD typed after a save is guarded again', async () => {
		requests.apiFetch.mockImplementation(async () => new Response('{"saved":true}'));
		const doc = target();
		seedDraft(doc, 'caf�');
		setDraftText(doc, 'cafe');
		await flushDraft(doc);
		setDraftText(doc, 'cafe au lait');
		await flushDraft(doc);
		expect(putBody()).toMatchObject({ text: 'cafe au lait', expected_sha256: sha('cafe') });
		clearDraft(doc);
	});
});
