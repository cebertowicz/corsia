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
	/** Which download is running or failed: the starter model for the person's own oMLX, or the full setup. */
	setup_kind?: 'starter' | 'setup' | 'settings';
	/** A Settings-initiated local download; its repository is retained so a durable failure can retry the same recommendation. */
	settings_download?: { name: string; repository: string | null; /** Memory the model needs once loaded; the download size when oMLX gives none. */ size_bytes?: number | null; percent: number | null; failed: boolean; /** Whether the model will be kept loaded (pinned) when it finishes: the backend's pin rule. */ will_pin?: boolean; /** The bytes are on disk; CORSIA is refreshing the catalog and loading the model. */ finishing?: boolean; /** On a failure: the bytes are on disk and only listing the model failed. */ downloaded?: boolean };
	/** The starter download's last reported percent; absent before one is known. */
	download_percent?: number | null;
	/** Remaining transfer time from measured throughput; absent until there is a sample. */
	download_eta_seconds?: number | null;
	/** True only when measured bytes have stopped advancing. */
	download_stalled?: boolean;
	/** Set on a stopped local source when the person cancelled the download, so no screen starts it again. */
	local_cancelled?: boolean;
	/** Why the local server isn't ready, as a code; `reason` is only its wording. */
	reason_code?: 'not_running' | 'no_models' | null;
	/** Set when an unusable CLI belongs to Homebrew or npm; setup leaves it to that manager. */
	cli_package_manager?: 'homebrew' | 'npm';
	/** Set when the CLI is installed but the service cannot see it; Fix links it. */
	invisible_binary?: string | null;
	accounts?: SourceAccount[];
	/** The conductor's bound account when it can no longer run here: removed, or turned off for the project. */
	lost_binding?: { label: string; reason: 'removed' | 'turned_off' } | null;
	/** Setup's managed local lifecycle and bounded failure facts. */
	local_state?: string;
	setup_error_code?: string;
	setup_error_reference?: string;
	bytes_to_free?: number;
	/** CORSIA's own local service exists, online or offline. */
	corsia_omlx?: boolean;
	/** CORSIA's own local service is online and can safely receive downloads. */
	corsia_omlx_online?: boolean;
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

export type LocalPickerRetry = 'starter' | 'setup' | 'settings';

/** Local setup ended in a failure the person can retry. */
export function localSetupFailed(source: ModelSource | undefined): boolean {
	return ['failed', 'setup_failed', 'start_failed'].includes(source?.local_state ?? '');
}

/** What the model picker's Local group says about a download in progress or a failed setup; null when neither. */
export function localPickerStatus(source: ModelSource | undefined): { line: string; retry: LocalPickerRetry | null } | null {
	if (!source) return null;
	const settings = source.settings_download;
	if (settings && !settings.failed) {
		if (settings.finishing) return { line: `Loading ${settings.name}`, retry: null };
		return { line: `Downloading ${settings.name} · ${settings.percent ?? 0}%`, retry: null };
	}
	if (settings?.failed) return {
		line: settings.downloaded ? `${settings.name} is downloaded but couldn’t be added.` : `Couldn’t download ${settings.name}.`,
		retry: settings.repository ? 'settings' : null,
	};
	if (source.local_state === 'downloading') {
		const percent = source.download_percent ?? null;
		if (source.download_stalled) return { line: percent === null ? 'Download paused' : `Download paused at ${percent}%`, retry: null };
		return { line: percent === null ? 'Downloading' : `Downloading ${percent}%`, retry: null };
	}
	if (localSetupFailed(source)) {
		return { line: 'Setup failed', retry: source.setup_kind === 'settings' ? null : source.setup_kind ?? null };
	}
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
};

export type LocalModelRecommendation = {
	name: string;
	repository: string;
	download_size_gb: number;
	context_window: number;
	/** Structured oMLX recommender facts; display wording belongs to localModelRecommendationMeta. */
	quantization_bits?: number;
	relative_speed?: number;
	/** oMLX's quality estimate, 0.5 to 5 dots, and its estimated decode speed. */
	smarts_level?: number;
	tokens_per_second?: number;
	/** Whether the model fits the memory free for a model now. */
	fits_now?: boolean;
	/** Memory oMLX says the model needs once loaded; absent when oMLX gives none. */
	resident_bytes?: number;
	disk?: { state: 'ready' | 'insufficient' | 'unknown'; free_bytes?: number; required_bytes?: number };
};

export type LocalModelDownload = { repository: string; model: string; status?: 'available' | 'failed'; speed?: number | null; server_id?: string | null };

export type LocalModelRecommendations = {
	state?: string;
	message?: string;
	/** Setup's pick (the recommended row) first, then fits-now models, then the rest, each by quality then speed. */
	models?: LocalModelRecommendation[];
	/** Durable Settings download records for the add-model sheet. */
	downloads?: LocalModelDownload[];
	/** Host facts returned with recommendations so the sheet names the same machine it evaluated. */
	memory_gb?: number;
	/** Memory a model may take now: free memory less the load reserve, the room setup's pick uses. */
	memory_room_gb?: number;
	/** The row setup would pick now, by the same fit rule; null when the list has none. */
	recommended_repository?: string | null;
	disk_free_gb?: number;
};

async function localRouteError(response: Response, fallback: string): Promise<Error> {
	const body = await response.json().catch(() => null);
	return Object.assign(new Error(typeof body?.detail === 'string' ? body.detail : fallback), { status: response.status });
}

/** Settings' local-model recommendations, checked by oMLX for this computer. */
export async function fetchLocalModelRecommendations(t: string): Promise<LocalModelRecommendations> {
	const response = await fetch('/api/providers/local/recommendations', { headers: { Authorization: `Bearer ${t}` } });
	if (!response.ok) throw await localRouteError(response, "Couldn’t check which models fit this computer.");
	return response.json();
}

/** Settings' cheap durable local-download projection. */
export async function fetchLocalModelDownloads(t: string): Promise<LocalModelDownload[]> {
	const response = await fetch('/api/providers/local/downloads', { headers: { Authorization: `Bearer ${t}` } });
	if (!response.ok) throw await localRouteError(response, "Couldn't read local model downloads. Try again.");
	return response.json();
}

/** Start a Settings-selected local model download. */
export async function startLocalModelDownload(t: string, repository: string): Promise<void> {
	const response = await fetch('/api/providers/local/downloads', {
		method: 'POST',
		headers: { Authorization: `Bearer ${t}`, 'Content-Type': 'application/json' },
		body: JSON.stringify({ repository }),
	});
	if (!response.ok) throw await localRouteError(response, "Couldn't start the download. Try again.");
}

/** Delete a completed Settings-selected local model download. */
export async function deleteLocalModelDownload(t: string, repository: string): Promise<void> {
	const response = await fetch(`/api/providers/local/downloads/${encodeURIComponent(repository)}`, {
		method: 'DELETE',
		headers: { Authorization: `Bearer ${t}` },
	});
	if (!response.ok) throw await localRouteError(response, "Couldn't delete the download. Try again.");
}

export async function cancelLocalDownload(t: string): Promise<{ ok: boolean; cancelled: boolean }> {
	const response = await fetch('/api/providers/local/cancel', {
		method: 'POST', headers: { Authorization: `Bearer ${t}` },
	});
	if (!response.ok) throw new Error(`local cancel ${response.status}`);
	return response.json();
}

export async function setupLocalSource(t: string): Promise<LocalSetupResult> {
	const response = await fetch('/api/providers/local/setup', {
		method: 'POST', headers: { Authorization: `Bearer ${t}`, 'Content-Type': 'application/json' },
		body: JSON.stringify({}),
	});
	const body = await response.json().catch(() => null);
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
