import { apiFetch } from '$lib/api/request';

export interface DocOwnership {
	owner: { kind: 'agent' | 'person' | 'none'; id: string | null; name: string | null };
	supervisor: { id: string; name: string; source: string; status: string } | null;
	can_act: boolean;
	multi_person: boolean;
	/** False on a document everyone shares while more than one person uses CORSIA: the server refuses any change. */
	can_change: boolean;
	choices: { id: string; name: string; status: string }[];
}

/** Carries the server's refusal sentence, when it sent one, so the control can show why. */
export class OwnershipChangeError extends Error {
	constructor(readonly status: number, readonly detail: string | null) { super(detail ?? 'Ownership change failed'); }
}

/** `owner` is left out when only the supervisor changes, so a supervisor pick never touches the owner. */
export async function changeOwnership(artifactId: string, change: { owner?: string; supervisor: string | null }): Promise<DocOwnership> {
	const body: Record<string, string | null> = { supervisor_thread_id: change.supervisor };
	if (change.owner !== undefined) body.owner = change.owner;
	const response = await apiFetch(`/api/artifacts/${encodeURIComponent(artifactId)}/ownership`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body),
	});
	if (!response.ok) {
		const detail = await response.json().then((value) => (typeof value?.detail === 'string' ? value.detail : null), () => null);
		throw new OwnershipChangeError(response.status, detail);
	}
	return (await response.json()).ownership;
}
