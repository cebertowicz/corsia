import { apiFetch } from '$lib/api/request';

export interface DocOwnership {
	owner: { kind: 'agent' | 'person' | 'none'; id: string | null; name: string | null; /** The word every person-facing surface shows for this owner. */ label: string };
	supervisor: { id: string; name: string; source: string; status: string } | null;
	can_act: boolean;
	multi_person: boolean;
	/** False on a document everyone shares while more than one person uses CORSIA: the server refuses any change. */
	can_change: boolean;
	/** The sentences the server refuses with, shown before a pick so the screen and the refusal read alike. */
	refusal: string | null;
	multi_person_refusal: string | null;
	/** Where the picker's agents come from. */
	scope: 'project' | 'workspace';
	choices: { id: string; name: string; status: string; responsibility: string }[];
}

/** Carries the server's refusal sentence, when it sent one, so the control can show why. */
export class OwnershipChangeError extends Error {
	constructor(readonly status: number, readonly detail: string | null) { super(detail ?? 'Ownership change failed'); }
}

/** Sends only the field being changed; the server keeps the other as it is. */
export async function changeOwnership(artifactId: string, change: { owner: string } | { supervisor: string }): Promise<DocOwnership> {
	const body = 'owner' in change ? { owner: change.owner } : { supervisor_thread_id: change.supervisor };
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
