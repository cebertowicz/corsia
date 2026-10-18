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
	let busy = $state(false);

	async function load() {
		loading = true;
		error = '';
		try {
			const result = await loadProviderPolicy(currentProjectId());
			if (result.ok && Array.isArray(result.value?.providers)) policy = result.value;
			else error = result.ok ? 'Could not load providers.' : result.error;
		} finally {
			loading = false;
		}
	}

	async function save(entry: ProviderEntry, patch: Partial<ProviderEntry>) {
		if (busy) return;
		busy = true;
		error = '';
		try {
			const result = await saveProviderPolicy({ ...entry, ...patch }, currentProjectId());
			if (result.ok && policy) {
				policy = { ...policy, providers: policy.providers.map((item) => item.provider_key === entry.provider_key ? result.value : item) };
			} else if (!result.ok) error = result.error;
		} finally {
			busy = false;
		}
	}

	async function toggleScope(useWorkspace: boolean) {
		const current = currentProjectId();
		if (!current || !policy || busy) return;
		busy = true;
		try {
			const result = await saveUseWorkspace(current, useWorkspace, policy.override_revision ?? 0);
			if (result.ok) policy = result.value;
			else error = result.error;
		} finally {
			busy = false;
		}
	}

	function setEnabled(provider: ProviderEntry, unitId: string, enabled: boolean) {
		void save(provider, { disabled: enabled ? provider.disabled.filter((id) => id !== unitId) : [...provider.disabled, unitId] });
	}

	function move(provider: ProviderEntry, unitId: string, direction: -1 | 1) {
		const order = [...(provider.order ?? provider.units.map((unit) => unit.id))];
		const index = order.indexOf(unitId);
		const destination = index + direction;
		if (index < 0 || destination < 0 || destination >= order.length) return;
		[order[index], order[destination]] = [order[destination], order[index]];
		void save(provider, { order });
	}

	return {
		get policy() { return policy; },
		get error() { return error; },
		get loading() { return loading; },
		get busy() { return busy; },
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
