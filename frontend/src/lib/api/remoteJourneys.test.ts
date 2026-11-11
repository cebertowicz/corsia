import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { token } from '$lib/stores/auth';
import { activeThreadId } from '$lib/stores/threads';
import { loadThreads } from '$lib/stores/threadsLoad';
import { loadRunRange } from '$lib/focusRunLoader';
import { fetchContent, fetchComments, fetchArtifactPane, postComment } from './docs';
import { answerReview, fetchReviewBar } from './reviewBar';
import { resetUploadLimits, uploadInputFile } from '$lib/components/inputBarHelpers';
import { loadUpdateStatus, resetUpdateStoreForTests, saveUpdateSchedule } from '$lib/update/store';
import { loadSettings, updateSettings } from '$lib/stores/settings';
import contract from './remoteJourneyCalls.json';

/**
 * The calls the app's main journeys make, as the Devices listener (the remote-apps entrance) sees them.
 *
 * Each journey is driven through the real modules with a recording `fetch`. `remoteJourneyCalls.json` is the
 * list of those calls; `backend/tests/test_devices_listener_journeys.py` replays every entry through the real
 * listener, so a call added to a journey here has to be added there and proved against the listener.
 */

const SESSION = 'person-session';

interface Seen {
	method: string;
	path: string;
	url: string;
	authorization: string | null;
}

function recordingFetch(seen: Seen[]) {
	return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		seen.push({
			method: (init?.method ?? 'GET').toUpperCase(),
			path: url.split('?')[0],
			url,
			authorization: new Headers(init?.headers).get('Authorization'),
		});
		return new Response(JSON.stringify({ threads: [], coordination: [], messages: [], comments: [], review: {} }), {
			status: 200,
			headers: { 'Content-Type': 'application/json' },
		});
	});
}

/** The ids the journeys use, as the contract's placeholders. */
function template(path: string): string {
	return path.replace('thread-1', '{thread}').replace('artifact-1', '{artifact}');
}

async function drive(name: string, journey: () => Promise<unknown>, seen: Seen[]): Promise<Seen[]> {
	const before = seen.length;
	// Only the requests matter here; a reply this minimal may fail a screen's own parsing afterwards.
	await journey().catch(() => undefined);
	const made = seen.slice(before);
	expect(made.length, `${name} made no call`).toBeGreaterThan(0);
	return made;
}

describe('the main journeys over the Devices listener', () => {
	let seen: Seen[];

	beforeEach(() => {
		seen = [];
		vi.stubGlobal('fetch', recordingFetch(seen));
		token.set(SESSION);
		resetUploadLimits();
		resetUpdateStoreForTests();
	});

	afterEach(() => {
		vi.unstubAllGlobals();
		token.set(null);
		activeThreadId.set(null);
	});

	async function runJourneys(): Promise<Record<string, Seen[]>> {
		const byJourney: Record<string, Seen[]> = {};
		activeThreadId.set('thread-1');
		const stub = { firstId: 'm1', lastId: 'm2', firstSeq: 1, lastSeq: 2, count: 2, turnIds: [], weight: 0, kinds: [] } as never;
		const deps = { pageSize: 20, loads: () => ({}), setLoad: () => undefined, onSummaries: () => undefined };
		byJourney['thread open'] = await drive('thread open', async () => {
			await loadThreads(SESSION);
			await loadRunRange(stub, deps);
			await fetchArtifactPane('thread-1');
		}, seen);
		byJourney['send'] = await drive('send', async () => {
			await uploadInputFile(new File(['hello'], 'note.txt', { type: 'text/plain' }), SESSION, 'thread-1');
		}, seen);
		byJourney['document open and comment'] = await drive('document open and comment', async () => {
			await fetchContent('notes/plan.md', { threadId: 'thread-1' });
			await fetchComments('artifact-1');
			await postComment('artifact-1', { body: 'Looks right.' });
		}, seen);
		byJourney['review bar approve'] = await drive('review bar approve', async () => {
			await fetchReviewBar('artifact-1');
			await answerReview('artifact-1', 'approved', '', ['hold-1']);
		}, seen);
		byJourney['update screen'] = await drive('update screen', async () => {
			await loadUpdateStatus();
			await saveUpdateSchedule({ automatic: false });
		}, seen);
		byJourney['settings'] = await drive('settings', async () => {
			await loadSettings();
			await updateSettings({ usage_sharing: true });
		}, seen);
		return byJourney;
	}

	it('sends every call with the session in the Authorization header, and never a different one in the address', async () => {
		await runJourneys();

		expect(seen.length).toBeGreaterThan(10);
		for (const call of seen) {
			expect(call.url, `${call.method} ${call.path}`).toMatch(/^\/api\//);
			expect(call.authorization, `${call.method} ${call.path}`).toBe(`Bearer ${SESSION}`);
			// The Devices listener refuses a request whose header and address name different sessions.
			const named = new URLSearchParams(call.url.split('?')[1] ?? '').getAll('token');
			expect(named.filter((value) => value !== SESSION), `${call.method} ${call.path}`).toEqual([]);
		}
	});

	it('makes exactly the calls the listener test replays', async () => {
		const byJourney = await runJourneys();

		const made = Object.entries(byJourney).flatMap(([journey, calls]) => (
			calls.map((call) => ({
				journey, method: call.method, path: template(call.path), session_in_address: /[?&]token=/.test(call.url),
			}))
		));
		const unique = made.filter((call, index) => (
			made.findIndex((other) => other.journey === call.journey && other.method === call.method && other.path === call.path) === index
		));
		expect(unique).toEqual(contract.calls);
	});
});
