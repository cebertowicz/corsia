import type { ProviderModel } from './providerModels';

/** What the local model rows ask the backend about pins and about memory: they render its verdicts, never compute them. */

/**
 * The one line under a row of CORSIA's own oMLX: Loading while a download finishes its load,
 * else the backend's ``keepLine`` (a failed keep attempt, pins full, or a pinned model that
 * doesn't fit now).
 */
export function keepLine(m: ProviderModel, finishing: boolean): string | null {
	return finishing ? 'Loading on this computer…' : m.keepLine ?? null;
}

/** A pinned model that isn't loaded and has a line saying why may be loaded anyway. */
export function canLoadAnyway(m: ProviderModel, finishing: boolean): boolean {
	return !!m.pinned && m.state === 'default' && !m.starting && !finishing && !!m.keepLine;
}

/** Writes oMLX's own pin on a model; false when it could not be written. */
export async function putPinned(token: string, serverId: string, modelId: string, pinned: boolean): Promise<boolean> {
	try {
		const res = await fetch(
			`/api/settings/network-servers/${serverId}/models/${encodeURIComponent(modelId)}/settings?token=${encodeURIComponent(token)}`,
			{ method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pinned }) },
		);
		return res.ok;
	} catch { return false; }
}

/** Polls fast while ``tick`` says a model is transitioning, slowly otherwise, and stops after a quiet spell. */
export function createModelsPoller(tick: () => Promise<boolean>) {
	const ACTIVE_MS = 250, IDLE_MS = 5000, QUIET_MS = 30_000;
	let handle: ReturnType<typeof setTimeout> | null = null;
	let polling = false;
	let lastActiveAt = Date.now();
	async function run() {
		if (!polling) return;
		const active = await tick();
		if (active) lastActiveAt = Date.now();
		if (Date.now() - lastActiveAt > QUIET_MS) { polling = false; handle = null; return; }
		handle = setTimeout(run, active ? ACTIVE_MS : IDLE_MS);
	}
	return {
		ensure() {
			if (polling) return;
			polling = true;
			lastActiveAt = Date.now();
			handle = setTimeout(run, ACTIVE_MS);
		},
		stop() { polling = false; if (handle) clearTimeout(handle); },
	};
}

/** What the memory check shows before a load that may not fit. */
export type PreloadInfo = {
	modelName: string;
	estimatedGb: number;
	freeGb: number;
	afterLoadGb: number;
	safeReserveGb: number;
	warning: string;
	onConfirm: () => Promise<void>;
	onCancel: () => void;
};

/** The backend's answer to "can this model load now"; null when it can't be asked. */
export async function checkLoad(token: string, estimatedGb: number, provider: string): Promise<Record<string, any> | null> {
	try {
		const res = await fetch(`/api/system/host-resources/check-load?token=${encodeURIComponent(token)}`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ estimated_gb: estimatedGb, provider }),
		});
		return await res.json();
	} catch { return null; }
}
