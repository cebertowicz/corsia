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
	/** The picked local model's context window; null before there is a pick. */
	context_window?: number | null;
	/** The picked local model's download size, formatted by the backend; null before there is a pick. */
	download_size?: string | null;
	setup_stage?: string;
	/** A download is running (the starter model for the person's own oMLX, or the full setup). */
	setup_downloading?: boolean;
	/** Which download is running or failed: the starter model for the person's own oMLX, or the full setup. */
	setup_kind?: 'starter' | 'setup';
	/** The starter download's last reported percent; absent before one is known. */
	download_percent?: number | null;
	/** Remaining transfer time from measured throughput; absent until there is a sample. */
	download_eta_seconds?: number | null;
	/** True only when measured bytes have stopped advancing. */
	download_stalled?: boolean;
	/** Why the local server isn't ready, as a code; `reason` is only its wording. */
	reason_code?: 'not_running' | 'no_models' | null;
	/** Set when an unusable CLI belongs to Homebrew or npm; setup leaves it to that manager. */
	cli_package_manager?: 'homebrew' | 'npm';
	/** Set when the CLI is installed but the service cannot see it; Fix links it. */
	invisible_binary?: string | null;
	/** A failed local setup's reason, kept until the next attempt. */
	setup_error?: string;
	accounts?: SourceAccount[];
	/** The conductor's bound account when it can no longer run here: removed, or turned off for the project. */
	lost_binding?: { label: string; reason: 'removed' | 'turned_off' } | null;
	/** Only for the person's own oMLX: whether CORSIA manages it (J9). */
	omlx_management?: OwnOmlxManagement | null;
}

/** `restore_failed` is a Stop managing that didn't finish; `restores` name what it puts back. */
export interface OwnOmlxManagement {
	state: 'managed' | 'not_managed' | 'restore_failed';
	/** Their oMLX's version while it is too old to say which model fits. */
	cannot_check: string | null;
	restores: string[];
	/** Stopping removes the admin key CORSIA read to manage it. */
	forgets_key: boolean;
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
		if (source.download_stalled) return { line: percent === null ? 'Download paused' : `Download paused at ${percent}%`, retry: null };
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

export async function cancelLocalDownload(t: string): Promise<{ ok: boolean; cancelled: boolean }> {
	const response = await fetch('/api/providers/local/cancel', {
		method: 'POST', headers: { Authorization: `Bearer ${t}` },
	});
	if (!response.ok) throw new Error(`local cancel ${response.status}`);
	return response.json();
}

/** `manageExisting` is Welcome's choice for the person's own oMLX; leave it out where Welcome didn't show it. */
export async function setupLocalSource(t: string, consentExisting = false, manageExisting?: boolean): Promise<LocalSetupResult> {
	const response = await fetch('/api/providers/local/setup', {
		method: 'POST', headers: { Authorization: `Bearer ${t}`, 'Content-Type': 'application/json' },
		body: JSON.stringify({ consent_existing: consentExisting, manage_existing: manageExisting }),
	});
	const body = await response.json().catch(() => null);
	const code = body?.detail?.code ?? body?.code;
	if (response.status === 409 && code === 'consent_required') return { ok: false, code };
	if (!response.ok) throw new Error(`local setup ${response.status}`);
	return body;
}

/** Settings' Let CORSIA manage it or Stop managing, for the person's own oMLX; a refusal's message is safe to show. */
export async function changeOwnOmlxManagement(t: string, action: 'start' | 'stop'): Promise<{ ok: boolean; message?: string }> {
	const response = await fetch(`/api/providers/sources/omlx/management/${action}`, { method: 'POST', headers: { Authorization: `Bearer ${t}` } });
	const body = await response.json().catch(() => null);
	if (response.ok) return { ok: body?.ok !== false, message: typeof body?.message === 'string' ? body.message : undefined };
	return { ok: false, message: typeof body?.detail === 'string' ? body.detail : undefined };
}

/** Re-run every source check once; concurrent callers share the server's run. */
export async function recheckSources(t: string): Promise<ModelSource[]> {
	return readSources(await fetch('/api/providers/sources/recheck', {
		method: 'POST',
		headers: { Authorization: `Bearer ${t}` },
	}));
}
