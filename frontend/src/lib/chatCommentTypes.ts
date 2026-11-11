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
	anchor_prefix: string;
	anchor_suffix: string;
	created_at: string;
	answers: ChatCommentAnswer[];
}

/** What a transcript row carries about chat comments. */
export interface ChatCommentRowFields {
	/** Server-computed transcript content after placed chat answers are removed. */
	chat_visible_content?: string;
	chat_comments?: ChatComment[];
	chat_answer_cids?: string[];
	/** The server settled this row's Turn as comment-only: it leaves no Worked receipt. */
	commentOnlyReceipt?: boolean;
}
