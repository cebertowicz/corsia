/** An agent a comment or reply goes to: its id says who it is, its name is only what the line shows. */
export type Recipient = { id: string; name: string };

/**
 * Who a comment or reply goes to: the agents the server named, then each agent tagged in the text that is not already among them.
 * Agents are told apart by id: a name typed in the text that belongs to an agent already named shows once, and two agents that share
 * a name are two recipients.
 */
export function recipientNames(named: Recipient[], tagged: Recipient[]): string[] {
	const seen = new Set<string>();
	const names: string[] = [];
	for (const agent of [...named, ...tagged]) {
		if (seen.has(agent.id)) continue;
		seen.add(agent.id);
		names.push(agent.name);
	}
	return names;
}
