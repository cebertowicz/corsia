import { localHostRefusal } from './localHostRefusal';

/** The model CORSIA's oMLX picked for this computer, as `/api/setup/local-model` reports it. */
export type LocalPickModel = { name: string; repository: string; download_size_bytes: number; download_size_gb: number; context_window: number };
export type LocalPick = {
	state: 'not_checked' | 'checking' | 'picked' | 'failed' | 'low_memory';
	model: LocalPickModel | null;
	/** Why there is no model, or why it is the starter model. */
	message: string | null;
	/** Stable cause and reference for an actionable preparation failure. */
	cause?: string;
	ref?: string;
};

export const LOW_MEMORY_LOCAL_MODEL_SENTENCE = 'Local models need at least 24 GB of memory, and this computer has less. Smaller models and future updates may bring them to 16 GB computers.';
export const LOW_MEMORY_LOCAL_MODEL_MESSAGE = `${LOW_MEMORY_LOCAL_MODEL_SENTENCE} Connect a subscription or an API provider.`;

const PICK = '/api/setup/local-model';
const FAILED: LocalPick = { state: 'failed', model: null, message: null };

export function needsSpace(pick: LocalPick | null, freeDiskGb: number | undefined): boolean {
	return Boolean(pick?.model) && (freeDiskGb ?? Infinity) < (pick?.model?.download_size_gb ?? 0);
}

export function noLocalModel(eligible: boolean | undefined, pick: LocalPick | null): boolean {
	return eligible === false || pick?.state === 'low_memory';
}

/** Start or retry the setup pick. A superseded response never replaces the current pick. */
export function createSetupLocalPick() {
	let pick = $state<LocalPick | null>(null);
	let request = 0;

	async function start() {
		const mine = ++request;
		pick = { state: 'checking', model: null, message: null };
		try {
			const response = await fetch(PICK, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
			const body = await response.json().catch(() => null);
			const refusal = localHostRefusal(body);
			const view = response.ok ? body as LocalPick : refusal ? { ...FAILED, message: refusal, cause: 'never_local_host' } : null;
			if (mine === request) pick = view ?? FAILED;
		} catch {
			if (mine === request) pick = FAILED;
		}
	}

	return { get pick() { return pick; }, start };
}
