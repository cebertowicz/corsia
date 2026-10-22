from memory.task_match import MAX_TERMS, NodeText, first_term_at, match, task_terms


def test_terms_put_title_first_and_drop_boilerplate():
    terms = task_terms(NodeText(
        "Fix the missed idle checkpoints",
        criteria="Tests pass and the idle timer survives passive input; see #2283.",
    ))

    assert terms.terms[:3] == ("missed", "idle", "checkpoints")
    assert terms.title_terms == {"missed", "idle", "checkpoints"}
    assert "fix" not in terms.terms and "tests" not in terms.terms
    assert "2283" in terms.terms and len(terms.terms) <= MAX_TERMS


def test_person_words_add_terms_after_the_title():
    terms = task_terms(NodeText("Export format", words="Use CSV columns for date and account."))

    assert terms.terms[:2] == ("export", "format")
    assert "csv" in terms.terms and "csv" not in terms.title_terms


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


def test_first_term_at_skips_a_term_inside_a_longer_word():
    text = "The catalog stays. Log rotation runs nightly."

    assert first_term_at(frozenset({"log"}), text) == text.index("Log")
