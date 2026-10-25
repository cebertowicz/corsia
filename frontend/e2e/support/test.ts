import { test as base } from '@playwright/test';
import { resetBackendToSeed, SEED_ENABLED } from './backendSeed';
import { setDeliveryPreference } from './heldComments';

const filesReset = new Set<string>();

/**
 * `test` for every spec. The suite runs its files serially against one backend, so each file starts from the seed
 * captured before the first spec (see `global-setup.ts`): the first test of a file returns the backend to it, with
 * the first run pending and no thread, comment batch, setting or session an earlier file left. Tests of one file
 * share that state. A file whose every test needs the seed (each ends the first run, say) sets `seedPerTest`.
 */
export const test = base.extend<{ seedPerTest: boolean; seededBackend: void; deliveryPreferenceOn: void }>({
	seedPerTest: [false, { option: true }],
	seededBackend: [
		async ({ seedPerTest }, use, testInfo) => {
			const file = `${testInfo.project.name}:${testInfo.file}`;
			if (SEED_ENABLED && (seedPerTest || !filesReset.has(file))) {
				await resetBackendToSeed();
				filesReset.add(file);
			}
			await use();
		},
		{ auto: true },
	],
	// The person's "Deliver comments immediately" choice lives on the server, so a test that turns it off must not leave it off.
	deliveryPreferenceOn: [
		async ({ request, seededBackend }, use) => {
			void seededBackend;
			if (SEED_ENABLED) await setDeliveryPreference(request, true);
			await use();
		},
		{ auto: true },
	],
});

export { expect } from '@playwright/test';
