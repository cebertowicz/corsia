import { test as base } from '@playwright/test';
import { TEST_TOKEN } from './corsia';

async function threadIds(request: import('@playwright/test').APIRequestContext): Promise<string[]> {
	const response = await request.get('/api/threads', { params: { token: TEST_TOKEN } });
	if (!response.ok()) return [];
	return ((await response.json()).threads as { id: string }[]).map((thread) => thread.id);
}

/**
 * `test` for every spec. The suite runs serially against one backend and one database, and the sidebar lists
 * "Needs you" rows first and windows its rows above 150, so conversations a spec leaves behind push the next spec's
 * fresh conversation out of the mounted rows. After each test this deletes the conversations that test created.
 * Conversations that exist before a test starts (a `beforeAll`, an earlier test of a serial file) are kept.
 */
export const test = base.extend<{ deleteCreatedThreads: void }>({
	deleteCreatedThreads: [
		async ({ request }, use) => {
			const before = new Set(await threadIds(request));
			await use();
			for (const id of await threadIds(request)) {
				if (!before.has(id)) await request.delete(`/api/threads/${id}`, { params: { token: TEST_TOKEN } });
			}
		},
		{ auto: true },
	],
});

export { expect } from '@playwright/test';
