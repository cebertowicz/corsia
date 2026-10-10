import { resolveParticipant, type Participant } from './participants';

const human: Participant = { id: 'steve', name: 'Steve', kind: 'user', color: '#000' };

describe('resolveParticipant', () => {
	it('maps legacy you/user to the human when author_kind is absent', () => {
		expect(resolveParticipant('you', human).kind).toBe('user');
		expect(resolveParticipant('user', human).name).toBe('Steve');
	});

	it('renders an agent handled "you" as an agent when author_kind=agent (2.1)', () => {
		const p = resolveParticipant('you', human, 'agent');
		expect(p.kind).toBe('agent');
		expect(p.name).not.toBe('Steve');
	});

	it('renders an agent handled "user" as an agent when author_kind=agent', () => {
		expect(resolveParticipant('user', human, 'agent').kind).toBe('agent');
	});

	it('resolves a known agent id to its participant', () => {
		expect(resolveParticipant('claude-code', human, 'agent').name).toBe('Claude Code');
	});

	it('falls back to an agent participant for an unknown handle', () => {
		const p = resolveParticipant('Project conductor', human, 'agent');
		expect(p.kind).toBe('agent');
		expect(p.name).toBe('Project conductor');
	});
});
