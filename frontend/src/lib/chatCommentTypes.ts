export type ChatCommentAnswer = {
	user_message_id: string;
	cid: string;
	text: string;
	created_at: string;
	unread: boolean;
} & (
	| { author_kind: 'user'; assistant_message_id?: never }
	| { author_kind: 'agent'; assistant_message_id: string }
);

export interface ChatComment {
	id: string;
	cid: string;
	user_message_id: string;
	quote: string;
	note: string;
	from_off: number;
	to_off: number;
	created_at: string;
	answers: ChatCommentAnswer[];
}
