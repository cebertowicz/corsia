import { expect, type APIRequestContext } from '@playwright/test';
import { TEST_TOKEN } from './corsia';

/**
 * Sets the persisted Focus setting through the settings API. Unlike the Settings UI it needs no page state, so it
 * also works from an afterEach after a skipped test or a failure that left a dialog open.
 */
export async function setFocusMode(request: APIRequestContext, enabled: boolean): Promise<void> {
	const updated = await request.patch('/api/settings', {
		headers: { Authorization: `Bearer ${TEST_TOKEN}` },
		data: { features: { focus_mode: enabled } },
	});
	expect(updated.ok(), await updated.text()).toBeTruthy();
}
