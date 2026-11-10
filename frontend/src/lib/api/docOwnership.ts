import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';

export interface DocOwnership {
	owner: { kind: 'agent' | 'person' | 'none'; id: string | null; name: string | null };
	supervisor: { id: string; name: string; source: string; status: string } | null;
	can_act: boolean;
	choices: { id: string; name: string; status: string }[];
}

export async function changeOwnership(artifactId: string, owner: string, supervisor: string | null): Promise<DocOwnership> {
	const response = await fetch(`/api/artifacts/${encodeURIComponent(artifactId)}/ownership`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${get(token) ?? ''}` },
		body: JSON.stringify({ owner, supervisor_thread_id: supervisor }),
	});
	if (!response.ok) {
		const body = await response.json().catch(() => ({}));
		throw new Error(typeof body.detail === 'string' ? body.detail : 'Could not change ownership.');
	}
	return (await response.json()).ownership;
}
