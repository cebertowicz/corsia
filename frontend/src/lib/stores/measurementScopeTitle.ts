import { derived } from 'svelte/store';
import { agentLabel } from './measurementAgentLabel';
import { measurementAgents } from './measurementAgents';
import { measurementScope } from './measurement';
import { projects } from './projects';
import { threads } from './threads';
import { uiScope } from './uiScope';

/** What a measured agent is called: its measured name, else its thread's title. */
export function measurementAgentName(
	agentId: string,
	measured: { agent: { id: string; name: string } }[],
	lanes: { id: string; title: string }[],
): string {
	return agentLabel(measured.find((row) => row.agent.id === agentId)?.agent.name
		?? lanes.find((thread) => thread.id === agentId)?.title
		?? 'Selected agent');
}

/**
 * The one name the Measurement page carries, for the app header: the agent when one
 * is open, All work, else the project. The scope owns it, never the thread that was
 * open before the page, so a project link cannot read as an agent.
 */
export const measurementScopeTitle = derived(
	[measurementScope, measurementAgents, projects, threads, uiScope],
	([$scope, $agents, $projects, $threads, $uiScope]) => {
		if ($scope.agent) return measurementAgentName($scope.agent, $agents.rows, $threads);
		if ($scope.project === 'all') return 'All work';
		return $projects.find((project) => project.id === $scope.project)?.name
			?? ($uiScope?.id === $scope.project ? $uiScope.name : 'Measurement');
	},
);
