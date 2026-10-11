export type FeatureSettings = {
	debug_version_overlay?: boolean;
	focus_mode?: boolean;
	cpu_profiler?: boolean;
};

export type SettingsPatch = {
	host_access_reviewer_model?: string | null;
	host_access_auto_approve?: boolean;
	host_access_reviewer_fallback?: boolean;
	live_bulletins?: { enabled: boolean };
	features?: FeatureSettings;
};

let featurePatchQueue = Promise.resolve();

/** Run feature writes in invocation order, even when the network reorders responses. */
export function enqueueFeaturePatch<T>(operation: () => Promise<T>): Promise<T> {
	const next = featurePatchQueue.then(operation, operation);
	featurePatchQueue = next.then(() => undefined, () => undefined);
	return next;
}

export function applyFeaturePatch<T extends { features?: FeatureSettings }>(
	current: T | null,
	updated: { features?: FeatureSettings },
): T | null {
	if (!current) return current;
	return {
		...current,
		// PATCH returns the full server feature truth; never carry an omitted Focus value forward.
		features: updated.features,
	};
}

export function confirmsFeaturePatch(patch: SettingsPatch, updated: { features?: FeatureSettings }): boolean {
	if (!updated.features || typeof updated.features !== 'object') return false;
	return Object.entries(patch.features ?? {}).every(([key, value]) => updated.features?.[key as keyof FeatureSettings] === value);
}

export class SettingsSnapshotGate {
	private latestRead = 0;
	private featureEpoch = 0;

	beginRead(): readonly [number, number] {
		return [++this.latestRead, this.featureEpoch];
	}

	acceptsRead(ticket: readonly [number, number]): boolean {
		return ticket[0] === this.latestRead && ticket[1] === this.featureEpoch;
	}

	acceptFeatureTruth(): void {
		this.featureEpoch += 1;
		this.latestRead += 1;
	}
}
