/** Receipt and state vectors never prove durability, especially for deletions. */
export class DocumentDurability {
	private localGeneration = 0;
	private ackGeneration = 0;
	private coveredGeneration = -1;
	private writtenGeneration = 0;
	private messages = new Map<string, number>();

	constructor(readonly storeId: string) {}

	get clean(): boolean { return this.coveredGeneration >= this.localGeneration; }

	localTransaction(): void { this.localGeneration++; }

	/** A local transaction no landed flush has written yet: the only work a flush from this tab has to do. */
	get unwritten(): boolean { return this.writtenGeneration < this.localGeneration; }

	/** A flush that started at this local generation landed, so every transaction up to it is in the file. */
	written(generation: number): void { this.writtenGeneration = Math.max(this.writtenGeneration, generation); }

	/** Retrying an immutable message must retain the generation of its original bytes. */
	messageBuilt(id: string): void {
		if (!this.messages.has(id)) this.messages.set(id, this.localGeneration);
	}

	acknowledge(storeId: string, ids: string[]): void {
		if (storeId !== this.storeId) return;
		this.ackGeneration++;
		for (const id of ids) {
			const generation = this.messages.get(id);
			if (generation === undefined) continue;
			this.coveredGeneration = Math.max(this.coveredGeneration, generation);
			this.messages.delete(id);
		}
	}

	snapshot(): { localGeneration: number; ackGeneration: number } {
		return { localGeneration: this.localGeneration, ackGeneration: this.ackGeneration };
	}
}
