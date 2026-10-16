/** The model CORSIA's oMLX picked for this computer, as `/api/setup/local-model` reports it. */
export type LocalPickModel = { name: string; repository: string; download_size_bytes: number; download_size_gb: number; context_window: number };
/** Welcome's local row: checking, the pick, or why there is none. */
export type LocalPick = {
	state: 'not_checked' | 'checking' | 'picked' | 'none_fits' | 'failed' | 'low_memory' | 'own_omlx';
	model: LocalPickModel | null;
	message: string | null;
};

const PICK = '/api/setup/local-model';
const WANTED_KEY = 'corsia.setup.local-ai-wanted';
const FAILED: LocalPick = { state: 'failed', model: null, message: null };

function storedWanted(): boolean | undefined {
	if (typeof window === 'undefined') return undefined;
	try {
		const value = window.sessionStorage.getItem(WANTED_KEY);
		return value === null ? undefined : value === 'true';
	} catch { return undefined; }
}

function rememberWanted(wanted: boolean) {
	if (typeof window === 'undefined') return;
	try { window.sessionStorage.setItem(WANTED_KEY, String(wanted)); } catch { /* setup still works without session storage */ }
}

/** Whether the picked model needs more room than this computer has free. */
export function needsSpace(pick: LocalPick | null, freeDiskGb: number | undefined): boolean {
	return Boolean(pick?.model) && (freeDiskGb ?? Infinity) < (pick?.model?.download_size_gb ?? 0);
}

/**
 * Welcome's local switch and the pick behind it. Welcome opening, the switch going on and
 * Try again start the pick; the switch going off stops it. The latest request wins.
 * Starting shows checking until the pick request answers, which it does once oMLX has
 * picked: nothing can push the pick to Welcome before the account exists.
 */
export function createSetupLocalPick() {
	let pick = $state<LocalPick | null>(null);
	let wanted = $state(true);
	let request = 0;

	async function send(path: string, starting: boolean) {
		const mine = ++request;
		if (starting) pick = { state: 'checking', model: null, message: null };
		try {
			const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
			const view = response.ok ? await response.json() as LocalPick : null;
			if (mine === request) pick = view ?? (starting ? FAILED : null);
		} catch {
			if (mine === request) pick = starting ? FAILED : null;
		}
	}

	const start = () => send(PICK, true);

	return {
		get pick() { return pick; },
		get wanted() { return wanted; },
		set wanted(next: boolean) { wanted = next; rememberWanted(next); },
		start,
		/** Welcome opened: the switch starts on where a local model can run. An oMLX CORSIA doesn't manage is never started a second time, so it gets no pick. */
		open(eligible: boolean, ownOmlx: boolean) {
			wanted = eligible && (storedWanted() ?? true);
			if (wanted && !ownOmlx) void start();
		},
		/** The switch moved; a person's own oMLX has no pick to start or stop. */
		change(next: boolean, ownOmlx: boolean) {
			wanted = next;
			rememberWanted(next);
			if (!ownOmlx) void (next ? start() : send(`${PICK}/stop`, false));
		},
		/**
		 * Whether Get started starts the download. It waits for the pick, so only while one is coming
		 * or here; a person's own oMLX is the flight's to handle (its starter model, or the consent step).
		 */
		downloads(freeDiskGb: number | undefined): boolean {
			return wanted && ['checking', 'picked', 'own_omlx'].includes(pick?.state ?? '') && !needsSpace(pick, freeDiskGb);
		},
	};
}
