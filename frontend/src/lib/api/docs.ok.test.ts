import { describe, expect, it } from 'vitest';
import { ok, RequestRefused } from './docs';
import { saveErrorMessage } from '$lib/stores/docSaveError';

const MOVED = {
	detail: "Couldn't save: this document is at projects/a-long-project-name/shared/planning/a-long-folder-name/a-document-with-a-long-name.md. Your changes are still in this tab. Copy them into the document there.",
};

function refused(body: string, status = 409): Response {
	return new Response(body, { status });
}

describe('ok() on a refused request', () => {
	it('keeps a JSON body whole, so a detail over 200 characters is still readable', async () => {
		const body = JSON.stringify(MOVED);
		expect(body.length).toBeGreaterThan(200);

		const error = await ok(refused(body)).catch((e) => e);

		expect(error).toBeInstanceOf(RequestRefused);
		expect(error.message).toBe(`409 ${body}`);
		expect(saveErrorMessage(error)).toBe(MOVED.detail);
	});

	it('still cuts a raw text body (an error page) to 200 characters', async () => {
		const error = await ok(refused('x'.repeat(500), 502)).catch((e) => e);

		expect(error.message).toBe(`502 ${'x'.repeat(200)}`);
		expect(error.kind).toBeNull();
	});

	it('keeps a short JSON body as before', async () => {
		const error = await ok(refused(JSON.stringify({ detail: 'File already exists' }))).catch((e) => e);

		expect(error.message).toBe('409 {"detail":"File already exists"}');
		expect(saveErrorMessage(error)).toBe('File already exists');
	});
});
