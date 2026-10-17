export type ContextSourceOwnerTarget =
	| 'project-settings'
	| 'thread-settings'
	| 'providers'
	| 'workspace-settings'
	| 'system'
	| 'document'
	| 'runtime-details';

type ContextSourceLike = {
	layer?: string;
	ownerSurface?: string;
	stableReference?: string;
};

function normalized(value: string | undefined): string {
	return (value ?? '').toLowerCase();
}

export function contextSourceOwnerTarget(source: ContextSourceLike): ContextSourceOwnerTarget | null {
	const layer = normalized(source.layer);
	const owner = normalized(source.ownerSurface);
	const reference = normalized(source.stableReference);

	if (owner.includes('document') && reference) {
		return 'document';
	}
	if (layer === 'corsia') return 'runtime-details';
	if (
		owner.includes('project') ||
		owner.includes('agent') ||
		reference.startsWith('project://') ||
		layer === 'project'
	) {
		return 'project-settings';
	}
	if (
		owner.includes('provider') ||
		owner.includes('model') ||
		reference.startsWith('model://') ||
		layer === 'model' ||
		layer === 'harness'
	) {
		return 'providers';
	}
	if (owner.includes('runtime') || reference.endsWith('/runtime-context')) {
		return 'runtime-details';
	}
	if (owner === 'workspace-settings' || layer === 'workspace') return 'workspace-settings';
	if (owner === 'context-builder' || owner.includes('system') || layer === 'system') return 'system';
	return null;
}

export function contextSourceActionLabel(source: ContextSourceLike): string {
	const target = contextSourceOwnerTarget(source);
	if (target === 'project-settings') return 'Open Project settings';
	if (target === 'thread-settings') return 'Open Thread settings';
	if (target === 'providers') return 'Open Providers';
	if (target === 'workspace-settings') return 'Open Context settings';
	if (target === 'system') return 'Open System';
	if (target === 'document') return 'Open Document';
	if (target === 'runtime-details') return 'Open details';
	return '';
}

export function contextSourceOwnerLabel(source: ContextSourceLike): string {
	const target = contextSourceOwnerTarget(source);
	if (target === 'project-settings') return 'Project settings';
	if (target === 'thread-settings') return 'Thread settings';
	if (target === 'providers') return 'Providers';
	if (target === 'workspace-settings') return 'Context settings';
	if (target === 'system') return 'System context';
	if (target === 'document') return 'Document';
	if (target === 'runtime-details') return 'Runtime context';
	return '';
}
