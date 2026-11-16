import { get } from 'svelte/store';
import { apiFetch } from '$lib/api/request';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';
import { updateStatus } from './store';
import type { UpdateSchedule } from './types';

type SchedulePatch = Partial<Pick<UpdateSchedule, 'automatic' | 'time'>> & { tonight?: boolean };

function showScheduleSaveError(patch: SchedulePatch): void {
	toasts.showAction("Couldn't save the schedule", {
		label: 'Retry', run: () => { void saveUpdateSchedule(patch); },
	});
}

export async function saveUpdateSchedule(patch: SchedulePatch): Promise<boolean> {
	const authToken = get(token);
	if (!authToken) {
		showScheduleSaveError(patch);
		return false;
	}
	try {
		const response = await apiFetch('/api/system/update-schedule', {
			method: 'PUT', headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(patch), cache: 'no-store',
		});
		if (!response.ok) throw new Error(`Schedule save returned ${response.status}`);
		const schedule = await response.json() as UpdateSchedule;
		updateStatus.update((current) => current ? { ...current, schedule } : current);
		return true;
	} catch {
		showScheduleSaveError(patch);
		return false;
	}
}
