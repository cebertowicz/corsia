import { expect } from '@playwright/test';

const BACKEND_URL = `http://127.0.0.1:${Number(process.env.CORSIA_E2E_BACKEND_PORT ?? 3110)}`;

/** Only the mock-mode backend (`e2e_app`) serves the seed routes; a real-mode run has no seed to return to. */
export const SEED_ENABLED = process.env.CORSIA_E2E_MOCK === '1';

async function post(path: string): Promise<void> {
	const response = await fetch(`${BACKEND_URL}${path}`, { method: 'POST' });
	if (!response.ok) throw new Error(`POST ${path} failed: ${response.status} ${await response.text()}`);
}

/** The serving process's boot id, or null while the backend is down (it is, briefly, during a reset). */
async function bootId(): Promise<string | null> {
	try {
		const response = await fetch(`${BACKEND_URL}/api/test/boot-id`);
		return response.ok ? ((await response.json()) as { boot_id: string }).boot_id : null;
	} catch {
		return null;
	}
}

/** Record the running backend as the seed every reset returns to. Call once, before the first spec. */
export async function captureSeed(): Promise<void> {
	await post('/api/test/seed');
}

/**
 * Return the backend to the captured seed: database, state root, workspace and in-memory state, with the first run
 * pending again. The backend restarts to do it, so this resolves once a new process is serving.
 */
export async function resetBackendToSeed(): Promise<void> {
	const before = await bootId();
	expect(before, 'the backend answers before it is reset').not.toBeNull();
	await post('/api/test/reset');
	await expect
		.poll(async () => {
			const now = await bootId();
			return now !== null && now !== before;
		}, { message: 'a restarted backend is serving', timeout: 60_000 })
		.toBe(true);
}
