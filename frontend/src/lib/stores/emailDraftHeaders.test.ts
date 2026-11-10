import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { emailDraftHeaders, noteDocumentLoad, noteLiveHeader } from './emailDraftHeaders';

const draft = (text: string) => ({ id: 'draft-1', header: { text, recipients: [] } });

beforeEach(() => emailDraftHeaders.set({}));

describe('the email-draft readings', () => {
	it('takes a load that found the draft record, and a later live frame’s reading of the same document', () => {
		noteDocumentLoad('a1', draft('To: dana@example.com'));
		noteLiveHeader('a1', draft('To: dana@example.com, sam@example.com'));
		expect(get(emailDraftHeaders).a1.header.text).toBe('To: dana@example.com, sam@example.com');
	});

	it('forgets a document whose load found no draft record, and leaves the others', () => {
		noteDocumentLoad('a1', draft('To: dana@example.com'));
		noteDocumentLoad('a2', draft('To: sam@example.com'));
		noteDocumentLoad('a1', null);
		expect(Object.keys(get(emailDraftHeaders))).toEqual(['a2']);
	});

	it('ignores a frame with no reading or no document, so an ordinary document never becomes an email', () => {
		noteLiveHeader('a1', undefined);
		noteLiveHeader(undefined, draft('To: x'));
		noteDocumentLoad(null, draft('To: x'));
		expect(get(emailDraftHeaders)).toEqual({});
	});
});
