import { describe, expect, it } from 'vitest';
import type { ServerComment } from '$lib/api/docs';
import { checkVersion, EarlierVersionPosted } from './commentVersion';

const row = (body: string, tagged_agent_ids: string[] = []) => ({ id: 'draft-1', body, tagged_agent_ids }) as unknown as ServerComment;

describe('whether a stored comment is the version a press sent', () => {
	it.each([
		['the same text', 'Too vague', 'Too vague'],
		['text that differs only in whitespace the server strips', 'Too vague', '\n Too vague \t\u3000'],
		["a separator Python's strip removes and JavaScript's trim keeps", 'Too vague', 'Too vague\u001c'],
		["a character JavaScript's trim removes and the server keeps", 'Too vague\ufeff', 'Too vague\ufeff'],
	])('%s is that version', (_case, stored, sent) => {
		expect(checkVersion(row(stored), { body: sent, tagged_agent_ids: [] }).id).toBe('draft-1');
	});

	it('compares tags as a set, as the server saves them', () => {
		expect(checkVersion(row('Hi', ['b', 'a']), { body: 'Hi', tagged_agent_ids: ['a', 'b', 'a'] }).id).toBe('draft-1');
	});

	it.each([
		['the text changed', row('Too vague'), { body: 'Too vague: name the owner', tagged_agent_ids: [] }],
		['only the tags changed', row('Too vague', ['a']), { body: 'Too vague', tagged_agent_ids: ['a', 'b'] }],
	])('%s: the stored comment is an earlier version, carried by the error', (_case, stored, sent) => {
		let caught: unknown;
		try { checkVersion(stored, sent); } catch (error) { caught = error; }
		expect(caught).toBeInstanceOf(EarlierVersionPosted);
		expect((caught as EarlierVersionPosted).row).toBe(stored);
		expect((caught as Error).message).toBe('Your earlier version was posted. Your changes are kept here.');
	});
});
