export interface ChatCommentAnswer {
	assistant_message_id: string;
	text: string;
	created_at: string;
	unread: boolean;
}

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
