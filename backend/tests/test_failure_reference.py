from infra.failure_reference import REFERENCE, new_error_reference, new_reference


ALPHABET = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_new_error_reference_is_six_crockford_base32_characters():
    reference = new_error_reference()

    assert len(reference) == 6
    assert set(reference) <= ALPHABET


def test_new_error_reference_is_not_constant():
    assert len({new_error_reference() for _ in range(10)}) > 1


def test_one_accept_rule_takes_every_minted_form():
    assert REFERENCE.match(new_error_reference())
    for prefix in ("cli", "local", "signin"):
        assert REFERENCE.match(new_reference(prefix))


def test_accept_rule_rejects_lookalikes():
    for candidate in ("7KQ2M", "7KQ2MXA", "7kq2mx", "7KQ2MI", "7KQ2MU", "api-7KQ2MX", "cli-" + "0" * 15):
        assert not REFERENCE.match(candidate)
