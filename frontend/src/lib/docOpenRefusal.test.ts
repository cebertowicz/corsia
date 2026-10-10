import { expect, it } from 'vitest';
import { DocOpenError, docOpenRefusal, SNAPSHOT_REFUSAL_MESSAGE } from './docOpenRefusal';

it('returns curated setup detail without changing the incoming frame or Error', () => {
	const frame = { code: 'setup_failed', title: '', message: 'private fixture setup reason' };
	const original = { ...frame };
	const detail = docOpenRefusal(frame)!;
	expect(detail).not.toBe(frame);
	expect(detail).toEqual({ code: 'setup_failed', title: '', message: "Live editing couldn't start for this document." });
	expect(frame).toEqual(original);
	const error = Object.assign(new Error(frame.message), frame);
	expect(docOpenRefusal(error)?.message).toBe(detail.message);
	expect(error.message).toBe(original.message);
	expect(new DocOpenError(frame as typeof detail).message).toBe(detail.message);
});

it('allows only the exact existing snapshot disclosure through setup curation', () => {
	const detail = { code: 'setup_failed' as const, title: '', message: SNAPSHOT_REFUSAL_MESSAGE };
	expect(docOpenRefusal(detail)?.message).toBe(SNAPSHOT_REFUSAL_MESSAGE);
	expect(new DocOpenError(detail).message).toBe(SNAPSHOT_REFUSAL_MESSAGE);
	const near = { ...detail, message: detail.message + ' private fixture diagnostic' };
	expect(docOpenRefusal(near)?.message).toBe("Live editing couldn't start for this document.");
	expect(new DocOpenError(near).message).not.toContain('private fixture');
});
