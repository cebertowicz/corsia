import {
	loadProviderPolicy,
	saveProviderPolicy,
	saveUseWorkspace,
	type ProviderEntry,
	type ProviderPolicy,
} from '$lib/api/providerPolicy';

/** One policy owner for both workspace account controls and project overrides. */
export function createProviderPolicyState(projectId?: string | (() => string | undefined)) {
	const currentProjectId = () => typeof projectId === 'function' ? projectId() : projectId;
	let policy = $state<ProviderPolicy | null>(null);
	let error = $state('');
	let loading = $state(true);
	let pendingTargets = $state<Set<string>>(new Set());
	let failedTargets = $state<Set<string>>(new Set());
	const providerQueues = new Map<string, Promise<void>>();
	type PolicyPatch = Partial<ProviderEntry> | ((entry: ProviderEntry) => Partial<ProviderEntry>);

	async function load() {
		loading = true;
		error = '';
		failedTargets = new Set();
		try {
			const result = await loadProviderPolicy(currentProjectId());
			if (result.ok && Array.isArray(result.value?.providers)) policy = result.value;
			else error = result.ok ? 'Could not load providers.' : result.error;
		} finally {
			loading = false;
		}
	}

	function setPending(target: string, pending: boolean) {
		const next = new Set(pendingTargets);
		if (pending) next.add(target);
		else next.delete(target);
		pendingTargets = next;
	}

	function setFailure(target: string, failed: boolean) {
		const next = new Set(failedTargets);
		if (failed) next.add(target);
		else next.delete(target);
		failedTargets = next;
	}

	function save(entry: ProviderEntry, patch: PolicyPatch, target = `provider:${entry.provider_key}`): Promise<void> {
		if (pendingTargets.has(target)) return Promise.resolve();
		setPending(target, true);
		setFailure(target, false);
		error = '';
		const previous = providerQueues.get(entry.provider_key) ?? Promise.resolve();
		let queued: Promise<void>;
		queued = previous.then(async () => {
			const current = policy?.providers.find((item) => item.provider_key === entry.provider_key) ?? entry;
			const resolvedPatch = typeof patch === 'function' ? patch(current) : patch;
			const result = await saveProviderPolicy({ ...current, ...resolvedPatch }, currentProjectId());
			if (result.ok && policy) {
				policy = { ...policy, providers: policy.providers.map((item) => item.provider_key === entry.provider_key ? result.value : item) };
			} else if (!result.ok) {
				error = result.error;
				setFailure(target, true);
			}
		}).finally(() => {
			setPending(target, false);
			if (providerQueues.get(entry.provider_key) === queued) providerQueues.delete(entry.provider_key);
		});
		providerQueues.set(entry.provider_key, queued);
		return queued;
	}

	async function toggleScope(useWorkspace: boolean) {
		const current = currentProjectId();
		const target = 'scope';
		if (!current || !policy || pendingTargets.has(target)) return;
		setPending(target, true);
		setFailure(target, false);
		error = '';
		try {
			const result = await saveUseWorkspace(current, useWorkspace, policy.override_revision ?? 0);
			if (result.ok) policy = result.value;
			else {
				error = result.error;
				setFailure(target, true);
			}
		} finally {
			setPending(target, false);
		}
	}

	function setEnabled(provider: ProviderEntry, unitId: string, enabled: boolean) {
		void save(provider, (current) => ({ disabled: enabled ? current.disabled.filter((id) => id !== unitId) : [...current.disabled, unitId] }), `unit:${provider.provider_key}:${unitId}`);
	}

	function move(provider: ProviderEntry, unitId: string, direction: -1 | 1) {
		void save(provider, (current) => {
			const order = [...(current.order ?? current.units.map((unit) => unit.id))];
			const index = order.indexOf(unitId);
			const destination = index + direction;
			if (index < 0 || destination < 0 || destination >= order.length) return {};
			[order[index], order[destination]] = [order[destination], order[index]];
			return { order };
		}, `order:${provider.provider_key}:${unitId}`);
	}

	function isPending(target: string): boolean { return pendingTargets.has(target); }
	function hasFailure(target: string): boolean { return failedTargets.has(target); }
	function hasControlFailure(): boolean { return failedTargets.size > 0; }

	return {
		get policy() { return policy; },
		get error() { return error; },
		get loading() { return loading; },
		get busy() { return pendingTargets.size > 0; },
		isPending,
		hasFailure,
		hasControlFailure,
		load,
		save,
		toggleScope,
		setEnabled,
		move,
	};
}

export function providerOverrideSummary(policy: ProviderPolicy | null): string {
	const projects = policy?.overriding_projects ?? [];
	if (!projects.length) return 'Every project uses these settings';
	return `Overridden in ${projects.length} ${projects.length === 1 ? 'project' : 'projects'}: ${projects.map((project) => project.name).join(', ')}`;
}
