export type SourceAccountState = 'ready' | 'limited' | 'signed_out' | 'unchecked' | 'no_usable_model';

/** One account of a Claude Code or Codex source, as the picker's account rows list it. */
export interface SourceAccount {
	account_id: string;
	label: string;
	label_verified: boolean;
	/** False only when the label is just the vendor: no email, no own name, no plan. */
	identified: boolean;
	state: SourceAccountState;
	resets_at: string | null;
	corsia_agent_binding: boolean;
}

/** One row of `GET /api/providers/sources` (#3308): one state and one fix. */
export interface ModelSource {
	source: string;
	name: string;
	state: 'ready' | 'needs_auth' | 'not_installed' | 'needs_setup' | 'unverified';
	prior_state?: string | null;
	reason?: string | null;
	fix?: string | null;
	checked_at?: string | null;
	limited?: boolean;
	resets_at?: string | null;
	eligible?: boolean;
	estimated_context_window?: number;
	/** The starter model's download size, formatted by the backend. */
	download_size?: string;
	setup_stage?: string;
	/** A download is running (the starter model for the person's own oMLX, or the full setup). */
	setup_downloading?: boolean;
	/** Which download is running or failed: the starter model for the person's own oMLX, or the full setup. */
	setup_kind?: 'starter' | 'setup';
	/** The starter download's last reported percent; absent before one is known. */
	download_percent?: number | null;
	/** Why the local server isn't ready, as a code; `reason` is only its wording. */
	reason_code?: 'not_running' | 'no_models' | null;
	/** Set when the CLI is installed but the service cannot see it; Fix links it. */
	invisible_binary?: string | null;
	/** A failed local setup's reason, kept until the next attempt. */
	setup_error?: string;
	accounts?: SourceAccount[];
	/** The conductor's bound account when it can no longer run here: removed, or turned off for the project. */
	lost_binding?: { label: string; reason: 'removed' | 'turned_off' } | null;
}

/** The harness sources: Claude Code and Codex. */
export const VENDORS: Record<string, { name: string; cli: string }> = {
	'claude-code': { name: 'Claude', cli: 'Claude Code' },
	codex: { name: 'ChatGPT', cli: 'Codex' },
};

export function isHarness(source: ModelSource): boolean {
	return source.source in VENDORS;
}

/** Whether a source can run a model right now (not limited, signed out or unchecked). */
export function sourceIsReady(source: ModelSource): boolean {
	if (isHarness(source) && source.accounts) return source.accounts.some((account) => account.state === 'ready');
	return source.state === 'ready' && !source.limited;
}

/** The heading's rule: some source is Ready. Nothing is preselected. */
export function anyReady(sources: ModelSource[]): boolean {
	return sources.some(sourceIsReady);
}

/** What the model picker's Local group says about a download in progress or a failed setup; null when neither. */
export function localPickerStatus(source: ModelSource | undefined): { line: string; retry: 'starter' | 'setup' | null } | null {
	if (!source) return null;
	if (source.setup_stage || source.setup_downloading) {
		const percent = source.download_percent ?? null;
		return { line: percent === null ? 'Downloading' : `Downloading ${percent}%`, retry: null };
	}
	if (source.setup_error) return { line: 'Setup failed', retry: source.setup_kind ?? null };
	return null;
}


async function readSources(res: Response): Promise<ModelSource[]> {
	if (!res.ok) throw new Error(`sources ${res.status}`);
	const body = await res.json();
	return Array.isArray(body?.sources) ? body.sources : [];
}

export async function fetchSources(t: string): Promise<ModelSource[]> {
	return readSources(await fetch('/api/providers/sources', {
		headers: { Authorization: `Bearer ${t}` },
	}));
}

export type LocalSetupResult = {
	ok: boolean;
	in_progress?: boolean;
	stage?: string;
	message?: string;
	next_action?: string;
	progress?: string[];
	code?: 'consent_required';
};

export async function setupLocalSource(t: string, consentExisting = false): Promise<LocalSetupResult> {
	const response = await fetch('/api/providers/local/setup', {
		method: 'POST', headers: { Authorization: `Bearer ${t}`, 'Content-Type': 'application/json' },
		body: JSON.stringify({ consent_existing: consentExisting }),
	});
	const body = await response.json().catch(() => null);
	const code = body?.detail?.code ?? body?.code;
	if (response.status === 409 && code === 'consent_required') return { ok: false, code };
	if (!response.ok) throw new Error(`local setup ${response.status}`);
	return body;
}

/** Re-run every source check once; concurrent callers share the server's run. */
export async function recheckSources(t: string): Promise<ModelSource[]> {
	return readSources(await fetch('/api/providers/sources/recheck', {
		method: 'POST',
		headers: { Authorization: `Bearer ${t}` },
	}));
}
