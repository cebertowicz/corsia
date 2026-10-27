import { describe, expect, it, vi } from 'vitest';

import { withReportAction, type SetupReportRow } from './setupReport';

describe('withReportAction', () => {
	const rows = (): SetupReportRow[] => [{ key: 'codex', name: 'Codex', reference: 'ref-1' }];

	it('adds Save a report only to a reportable row that has a reference', () => {
		expect(withReportAction({ key: 'codex', name: 'Codex', meta: '', reportable: true, reference: 'ref-1', more: [{ label: 'Other', run: () => undefined }] }, rows).more?.map((item) => item.label)).toEqual(['Other', 'Save a report']);
		expect(withReportAction({ key: 'codex', name: 'Codex', meta: '', reportable: true }, rows).more).toBeUndefined();
		expect(withReportAction({ key: 'codex', name: 'Codex', meta: '', reference: 'ref-1' }, rows).more).toBeUndefined();
	});

	it('reads the rows when the report is saved, not when the row is built', () => {
		const read = vi.fn(rows);
		const row = withReportAction({ key: 'codex', name: 'Codex', meta: '', reportable: true, reference: 'ref-1' }, read);
		expect(read).not.toHaveBeenCalled();
		row.more?.[0].run();
		expect(read).toHaveBeenCalledOnce();
	});
});
