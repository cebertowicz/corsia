import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { plainErrorText, SERVER_UNREACHABLE } from '$lib/plainError';

export type ProviderEntry = {
	provider_key: string;
	capabilities: { units: 'accounts' | 'models' | 'self'; rollover: boolean };
	rollover: boolean;
	order: string[] | null;
	disabled: string[];
	revision: number;
	units: { id: string; label: string; enabled: boolean }[];
};
export type ProviderPolicy = {
	scope: 'workspace' | 'project';
	providers: ProviderEntry[];
	use_workspace?: boolean;
	override_revision?: number;
	overriding_projects?: { id: string; name: string }[];
};
export type ProjectCloudModelsPolicy = { cloud_models_off: boolean };
export type Result<T> = { ok: true; value: T } | { ok: false; error: string; conflict?: boolean };

async function request<T>(path: string, init?: RequestInit, refused = 'Could not save subscriptions.'): Promise<Result<T>> {
	const auth = get(token);
	if (!auth) return { ok: false, error: 'Sign in to manage subscriptions.' };
	try {
		const response = await fetch(path, { ...init, headers: { Authorization: `Bearer ${auth}`, 'Content-Type': 'application/json', ...(init?.headers ?? {}) } });
		const data = await response.json().catch(() => null);
		if (!response.ok) return { ok: false, conflict: response.status === 409, error: plainErrorText(data?.detail, refused) };
		return { ok: true, value: data as T };
	} catch { return { ok: false, error: SERVER_UNREACHABLE }; }
}

export function loadProviderPolicy(projectId?: string) {
	return request<ProviderPolicy>(`/api/provider-policy${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''}`);
}
export function saveProviderPolicy(provider: ProviderEntry, projectId?: string) {
	return request<ProviderEntry>(`/api/provider-policy/${encodeURIComponent(provider.provider_key)}${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''}`, {
		method: 'PUT', body: JSON.stringify({ expected_revision: provider.revision, rollover: provider.rollover, order: provider.order, disabled: provider.disabled }),
	});
}
export function saveUseWorkspace(projectId: string, useWorkspace: boolean, expectedRevision: number) {
	return request<ProviderPolicy>(`/api/projects/${encodeURIComponent(projectId)}/provider-policy/use-workspace`, {
		method: 'PUT', body: JSON.stringify({ use_workspace: useWorkspace, expected_revision: expectedRevision }),
	});
}
export function loadCloudModelsOff(projectId: string) {
	return request<ProjectCloudModelsPolicy>(`/api/projects/${encodeURIComponent(projectId)}/cloud-models-off`);
}
export function saveCloudModelsOff(projectId: string, cloudModelsOff: boolean) {
	return request<ProjectCloudModelsPolicy>(`/api/projects/${encodeURIComponent(projectId)}/cloud-models-off`, {
		method: 'PUT', body: JSON.stringify({ cloud_models_off: cloudModelsOff }),
	}, 'The server did not accept the change. Try again.');
}
