from infra.error_reference import new_error_reference


ALPHABET = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_new_error_reference_is_six_crockford_base32_characters():
    reference = new_error_reference()

    assert len(reference) == 6
    assert set(reference) <= ALPHABET


def test_new_error_reference_is_not_constant():
    assert len({new_error_reference() for _ in range(10)}) > 1
