import { apiFetch } from './request';
export type SkillScope = 'workspace' | 'project' | 'thread';

export interface SkillRow {
	name: string;
	description: string;
	scope: SkillScope;
	scope_id: string;
	path: string;
	body_path: string;
	metadata: Record<string, unknown>;
	status: 'included' | 'omitted' | 'error';
	errors: string[];
	overrides?: string | null;
}

async function ok(res: Response) {
	if (!res.ok) {
		const body = await res.json().catch(() => null);
		const detail = body?.detail;
		if (typeof detail === 'string') throw new Error(detail);
		if (detail?.message && Array.isArray(detail.errors)) {
			throw new Error(`${detail.message}: ${detail.errors.join(', ')}`);
		}
		throw new Error(`Request failed (${res.status})`);
	}
	return res.json();
}

export async function fetchSkills(params: {
	scope: SkillScope;
	projectId?: string | null;
	threadId?: string | null;
}): Promise<{ skills: SkillRow[]; root: string; scope_id: string }> {
	const q = new URLSearchParams({
		scope: params.scope,
	});
	if (params.projectId) q.set('project_id', params.projectId);
	if (params.threadId) q.set('thread_id', params.threadId);
	return ok(await apiFetch(`/api/skills?${q.toString()}`, { sessionInAddress: true }));
}

export async function createSkill(params: {
	scope: SkillScope;
	name: string;
	description: string;
	projectId?: string | null;
	threadId?: string | null;
}): Promise<SkillRow> {
	const data = await ok(await apiFetch(`/api/skills`, {
		sessionInAddress: true,
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			scope: params.scope,
			name: params.name,
			description: params.description,
			project_id: params.projectId ?? null,
			thread_id: params.threadId ?? null,
		}),
	}));
	return data.skill;
}
