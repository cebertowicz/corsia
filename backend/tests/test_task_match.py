from memory.task_match import MAX_TERMS, NodeText, match, task_terms


def test_terms_put_title_first_and_drop_boilerplate():
    terms = task_terms(NodeText(
        "Fix the missed idle checkpoints",
        criteria="Tests pass and the idle timer survives passive input; see #2283.",
    ))

    assert terms.terms[:3] == ("missed", "idle", "checkpoints")
    assert terms.title_terms == {"missed", "idle", "checkpoints"}
    assert "fix" not in terms.terms and "tests" not in terms.terms
    assert "2283" in terms.terms and len(terms.terms) <= MAX_TERMS


def test_node_text_has_no_description_and_uses_person_words():
    text = NodeText("Export format", words="Use CSV columns for date and account.")

    assert not hasattr(text, "description")
    assert "export" in task_terms(text).terms


def test_long_multi_topic_message_scores_its_matching_sentence_run_only():
    terms = task_terms(NodeText("Export account ledger", criteria="CSV format"))
    unrelated = "Unrelated roadmap notes. " * 30
    words = unrelated + "Export the account ledger as a CSV file with stable columns. " + unrelated

    found = match(terms, words)

    assert found is not None
    assert found.shared >= 2 and found.title_shared >= 1
    assert "CSV file" in found.span
    assert len(found.span) <= 400


def test_match_returns_counts_and_rejects_scattered_topics():
    terms = task_terms(NodeText("Export account ledger", criteria="CSV format"))
    words = "Export policy discussion.\n\nAccount settings discussion.\n\nLedger retention discussion."

    assert match(terms, words) is None
