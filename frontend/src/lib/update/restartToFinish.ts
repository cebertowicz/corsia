import { get } from 'svelte/store';
import { readSystemVersion, restartSystem, waitForSystemRestart } from '$lib/stores/settingsSystemRestart';
import { isFailedOperation } from './model';
import { markOutcomeSeen } from './outcome';
import { updateFailureVerdict, updateStarting, updateStatus } from './store';

/** An update was staged but never restarted: the person's next step is a plain restart, which activates it. */
export async function restartToFinishUpdate(): Promise<void> {
	const failed = get(updateStatus)?.operation;
	if (isFailedOperation(failed)) void markOutcomeSeen(failed.operation_id);
	updateFailureVerdict.set(null);
	updateStarting.set(true);
	try {
		const before = await readSystemVersion();
		const result = await restartSystem('restart');
		const done = result.ok ? await waitForSystemRestart(before, result.operation_id) : result;
		if (!done.ok) updateFailureVerdict.set({ kind: 'refused', message: done.message ?? "CORSIA couldn't restart." });
	} finally {
		updateStarting.set(false);
	}
}
