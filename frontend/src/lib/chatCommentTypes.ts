/** The server's delivery of the input that carried a comment or a person's reply; a held or ended one shows in its thread as chat shows it. */
export type Delivery = {
	delivery_state?: string | null; delivery_error?: string | null;
	/** With `active_turn_id`, what tells a held queued input from one being sent (messageDelivery.deliveryPresentation). */
	write_outcome?: 'accepted' | 'proved_no_write' | 'ambiguous' | null; active_turn_id?: string | null; delivering_reason?: string | null;
};

export type ChatCommentAnswer = {
	user_message_id: string;
	cid: string;
	text: string;
	created_at: string;
	unread: boolean;
} & (
	| ({ author_kind: 'user'; assistant_message_id?: never } & Delivery)
	| { author_kind: 'agent'; assistant_message_id: string }
);

export interface ChatComment extends Delivery {
	id: string;
	cid: string;
	user_message_id: string;
	quote: string;
	note: string;
	from_off: number;
	to_off: number;
	anchor_prefix: string;
	anchor_suffix: string;
	created_at: string;
	answers: ChatCommentAnswer[];
}
