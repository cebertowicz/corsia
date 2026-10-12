import { writable, get } from 'svelte/store';
import { token } from './auth';
import { plainErrorText, SERVER_UNREACHABLE } from '../plainError';
import { applyFeaturePatch, confirmsFeaturePatch, enqueueFeaturePatch, type FeatureSettings, type SettingsPatch, SettingsSnapshotGate } from './settingsFeaturePatch';

export interface CoreSettings {
	host_access_reviewer_model?: string | null;
	host_access_reviewer_model_name?: string | null;
	host_access_reviewer_status?: 'ready' | 'requester_model' | 'unavailable';
	host_access_reviewer_message?: string | null;
	host_access_auto_approve?: boolean;
	host_access_reviewer_fallback?: boolean;
	live_bulletins?: { enabled: boolean; base_url?: string };
	workspace_context_files?: WorkspaceContextFile[];
	features?: FeatureSettings;
}
export interface WorkspaceContextFile {
	source_id: string; name: string; label: string; path: string; exists: boolean;
	editable: boolean; owner_surface: string; size?: number | null; mtime?: number | null;
}

export interface ModelProfile {
	model_id: string; context_window: number | null;
	context_window_override?: number | null; // saved value only; context_window may be discovered
	context_window_source?: 'saved' | 'default' | 'provider' | null; // which one context_window is
	context_window_unsaved?: number | null; // what clearing a saved window lands on
	context_window_unsaved_source?: 'default' | 'provider' | null;
	parallel_slots: number; reasoning_budget: number; flash_attention: number; custom_args: string | null;
}
export const coreSettings = writable<CoreSettings | null>(null);
export const settingsLoading = writable(false);
export const settingsLoadError = writable(false);
const settingsSnapshotGate = new SettingsSnapshotGate();
const BASE = '';
export async function loadSettings(): Promise<void> {
	const ticket = settingsSnapshotGate.beginRead();
	settingsLoading.set(true);
	settingsLoadError.set(false);
	try {
		const res = await fetch(`${BASE}/api/settings`, {
			headers: { Authorization: `Bearer ${get(token)}` },
		});
		if (!res.ok) throw new Error(`settings read failed (${res.status})`);
		const snapshot = await res.json();
		if (settingsSnapshotGate.acceptsRead(ticket)) coreSettings.set({
			...snapshot,
			live_bulletins: snapshot.live_bulletins && typeof snapshot.live_bulletins === 'object' ? snapshot.live_bulletins : undefined,
			features: snapshot.features && typeof snapshot.features === 'object' ? snapshot.features : undefined,
		});
	} catch (e) {
		if (settingsSnapshotGate.acceptsRead(ticket)) {
			settingsLoadError.set(true);
			console.error('Failed to load settings:', e);
		}
	} finally {
		if (settingsSnapshotGate.acceptsRead(ticket)) settingsLoading.set(false);
	}
}
export function updateSettings(patch: SettingsPatch): Promise<{ ok: boolean; error?: string }> {
	return patch.features ? enqueueFeaturePatch(() => submitSettingsPatch(patch)) : submitSettingsPatch(patch);
}
async function submitSettingsPatch(patch: SettingsPatch): Promise<{ ok: boolean; error?: string }> {
	try {
		const res = await fetch(`${BASE}/api/settings`, {
			method: 'PATCH',
			headers: {
				Authorization: `Bearer ${get(token)}`,
				'Content-Type': 'application/json',
			},
			body: JSON.stringify(patch),
		});
		if (res.ok) {
			const updated = await res.json().catch(() => ({}));
			if (patch.features) {
				if (!confirmsFeaturePatch(patch, updated)) {
					return { ok: false, error: 'Settings did not confirm the saved feature value.' };
				}
				settingsSnapshotGate.acceptFeatureTruth();
				settingsLoading.set(false);
				settingsLoadError.set(false);
				coreSettings.update((current) => applyFeaturePatch(current, updated));
				return { ok: true };
			}
			// The PATCH reply is the stored value; show it even if the refresh below fails.
			if (typeof updated?.live_bulletins?.enabled === 'boolean') {
				coreSettings.update((current) => ({ ...current, live_bulletins: updated.live_bulletins }));
			}
			await loadSettings();
			return { ok: true };
		}
		const data = await res.json().catch(() => null);
		if (data?.detail !== undefined) console.error('[settings] update rejected', data.detail);
		return {
			ok: false,
			error: plainErrorText(data?.detail, 'Could not save these settings. Try again.'),
		};
	} catch (e) {
		console.error('Failed to update settings:', e);
		return { ok: false, error: SERVER_UNREACHABLE };
	}
}

export interface UpdateStatus {
	branch: string | null;
	current_sha: string | null;
	remote_sha: string | null;
	commits_behind: number | null;
	available: boolean | null;
	checked_at: number | null;
	error: string | null;
}

export const updateStatus = writable<UpdateStatus | null>(null);

/** Refresh the cached update status. `refresh` forces a new remote probe. */
export async function loadUpdateStatus(refresh = false): Promise<void> {
	const t = get(token);
	if (!t) return;
	try {
		const res = await fetch(
			`${BASE}/api/system/update-status?refresh=${refresh ? 'true' : 'false'}`,
			{ headers: { Authorization: `Bearer ${t}` } },
		);
		if (!res.ok) throw new Error(`Update check returned ${res.status}`);
		updateStatus.set(await res.json());
	} catch (e) {
		// A failed check must not leave a confident stale answer on screen. The
		// endpoint never claims "up to date" without evidence, and neither does
		// the client: drop to unknown, keeping the last known context so the
		// System card can still say what was checked and why it failed.
		updateStatus.update((prev) => ({
			branch: prev?.branch ?? null,
			current_sha: prev?.current_sha ?? null,
			remote_sha: prev?.remote_sha ?? null,
			commits_behind: null,
			available: null,
			checked_at: prev?.checked_at ?? null,
			error: e instanceof Error ? e.message : 'Update check failed.',
		}));
	}
}

export async function loadModelProfile(modelId: string): Promise<ModelProfile | null> {
	try {
		const res = await fetch(`${BASE}/api/models/${modelId}/profile`);
		if (res.ok) return await res.json();
		return null;
	} catch {
		return null;
	}
}

export async function updateModelProfile(modelId: string, patch: Partial<ModelProfile>): Promise<boolean> {
	try {
		const t = get(token);
		const res = await fetch(`${BASE}/api/models/${modelId}/profile?token=${t}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(patch),
		});
		return res.ok;
	} catch {
		return false;
	}
}
