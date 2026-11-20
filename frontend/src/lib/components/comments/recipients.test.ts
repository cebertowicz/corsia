import { describe, expect, it } from 'vitest';
import { recipientNames } from './recipients';

describe('recipientNames', () => {
	it('shows an agent once when it is named and also tagged by a typed name', () => {
		expect(recipientNames([{ id: 'docs', name: 'Docs lane' }], [{ id: 'docs', name: 'docs lane' }])).toEqual(['Docs lane']);
	});
	it('shows two agents that share a name twice', () => {
		expect(recipientNames([{ id: 'a1', name: 'Docs lane' }], [{ id: 'a2', name: 'Docs lane' }])).toEqual(['Docs lane', 'Docs lane']);
	});
	it('adds a tagged agent the server did not name, after the named ones', () => {
		expect(recipientNames([{ id: 'a1', name: 'Docs lane' }], [{ id: 'a3', name: 'Release lane' }])).toEqual(['Docs lane', 'Release lane']);
	});
});
