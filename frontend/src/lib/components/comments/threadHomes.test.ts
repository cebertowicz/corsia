import { describe, expect, it, vi } from 'vitest';
import { createThreadHomes } from './threadHomes';

describe('createThreadHomes', () => {
	it("sends a thread's stamp to the home it was first shown in, whatever is shown later", async () => {
		const homes = createThreadHomes();
		homes.note(['a', 'b'], 'doc-1');
		homes.note(['a', 'c'], 'doc-2');
		const send = vi.fn().mockResolvedValue(undefined);
		await homes.stamp('a', send);
		await homes.stamp('c', send);
		expect(send.mock.calls).toEqual([['doc-1'], ['doc-2']]);
	});

	it('sends nothing, and does not throw, for a thread with no home noted (never shown here, or gone before it was)', async () => {
		const homes = createThreadHomes();
		homes.note(['a'], null);
		const send = vi.fn().mockResolvedValue(undefined);
		await expect(homes.stamp('a', send)).resolves.toBeUndefined();
		await expect(homes.stamp('missing', send)).resolves.toBeUndefined();
		expect(send).not.toHaveBeenCalled();
		expect(homes.of('missing')).toBeUndefined();
	});
});
