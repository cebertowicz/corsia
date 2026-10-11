/** An agent as the Measurement page names it: the lane's own name with its first letter capitalised ("conductor" reads "Conductor"). */
export function agentLabel(name: string): string {
	return name.charAt(0).toUpperCase() + name.slice(1);
}
