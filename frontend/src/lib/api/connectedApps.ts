import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { plainResponseError } from '$lib/plainError';

export type AppState = 'awaiting_approval' | 'connecting' | 'working' | 'needs_token' | 'token_rejected' | 'needs_sign_in' | 'disconnected' | 'failed' | 'expired';

export interface ConnectedApp {
	id: string;
	name: string;
	state: AppState | string;
	revision: number;
	tool_count?: number;
	failure_class?: string | null;
	reach: { mode: 'all' | 'selected'; project_ids: string[]; outside_projects: boolean };
}

function url(path: string): string {
	const auth = get(token);
	return `${path}?token=${encodeURIComponent(auth ?? '')}`;
}

async function response<T>(path: string, init?: RequestInit): Promise<T> {
	const result = await fetch(url(path), init);
	if (!result.ok) throw new Error(await plainResponseError(result, 'Could not update this app. Try again.'));
	return result.json() as Promise<T>;
}

export async function loadConnectedApps(): Promise<ConnectedApp[]> {
	const result = await response<{ servers: ConnectedApp[] }>('/api/mcp/servers');
	return result.servers;
}

export function approveConnectedApp(id: string): Promise<ConnectedApp> {
	return response(`/api/mcp/servers/${encodeURIComponent(id)}/approve`, { method: 'POST' });
}

export function declineConnectedApp(id: string): Promise<ConnectedApp> {
	return response(`/api/mcp/servers/${encodeURIComponent(id)}/decline`, { method: 'POST' });
}

export function reconnectConnectedApp(id: string): Promise<ConnectedApp> {
	return response(`/api/mcp/servers/${encodeURIComponent(id)}/reconnect`, { method: 'POST' });
}

export function removeConnectedApp(id: string): Promise<void> {
	return response(`/api/mcp/servers/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export function saveConnectedAppReach(app: ConnectedApp, reach: ConnectedApp['reach']): Promise<ConnectedApp> {
	return response(`/api/mcp/servers/${encodeURIComponent(app.id)}/reach`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ reach, expected_revision: app.revision }),
	});
}
