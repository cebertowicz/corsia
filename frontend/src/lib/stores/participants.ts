import { writable, derived } from 'svelte/store';
import { user } from './auth';

export type ParticipantKind = 'user' | 'agent';
export type Provider = 'claude-code' | 'codex' | 'api' | 'local';

export interface Participant {
	id: string;
	name: string;
	kind: ParticipantKind;
	color: string;
	provider?: Provider;
	model?: string;
	thread?: string;
}

export const userName = writable('');
export const userParticipant = derived([userName, user], ([$n, $u]): Participant => ({
	id: 'you',
	name: $n || $u?.name || 'You',
	kind: 'user',
	color: 'var(--text-tertiary)'
}));

export const AGENTS: Record<string, Participant> = {
	jarvis:           { id: 'jarvis',         name: 'Jarvis',      kind: 'agent', color: '#e0489e', provider: 'api',         model: 'opus-4.8',    thread: 'this chat' },
	'jarvis-sonnet':  { id: 'jarvis-sonnet',  name: 'Jarvis',      kind: 'agent', color: '#e0489e', provider: 'api',         model: 'sonnet-4.6',  thread: 'this chat' },
	conductor:        { id: 'conductor',      name: 'Conductor',   kind: 'agent', color: '#8b5cf6', provider: 'claude-code', model: 'opus-4.8',    thread: 'project coordination' },
	'claude-code':    { id: 'claude-code',    name: 'Claude Code', kind: 'agent', color: '#2dd4bf', provider: 'claude-code', model: 'opus-4.8',    thread: 'documents' },
	codex:            { id: 'codex',          name: 'Codex',       kind: 'agent', color: '#60a5fa', provider: 'codex',       model: 'gpt-5-codex', thread: 'implementation' }
};

const agentFor = (id: string): Participant =>
	AGENTS[id] ?? { id, name: id, kind: 'agent', color: 'var(--text-tertiary)' };

export function resolveParticipant(id: string, usr: Participant, kind?: ParticipantKind): Participant {
	if (kind === 'agent') return agentFor(id);
	if (id === 'you' || id === 'user') return usr;
	if (id === 'agent') return AGENTS.jarvis;
	return agentFor(id);
}

export function agentTag(p: Participant): string {
	if (p.provider === 'claude-code') return p.name === 'Claude Code' ? '' : 'via Claude Code';
	if (p.provider === 'codex') return p.name === 'Codex' ? '' : 'via Codex';
	return p.model ?? '';
}
