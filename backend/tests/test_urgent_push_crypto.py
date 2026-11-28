"""R8 (design round 6): encryption and the VAPID signature, against RFC 8291's published values."""
from __future__ import annotations

import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from infra.push_crypto import encrypt, private_from_bytes, public_bytes, unb64u, vapid_token


def test_encryption_matches_rfc_8291_appendix_a():
    """Fixed keys, salt and plaintext give the 86-octet header and the ciphertext the RFC publishes."""
    body = encrypt(
        unb64u("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
        unb64u("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
        unb64u("BTBZMqHH6r4Tts7J_aSIgg"),
        as_private=private_from_bytes(unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw")),
        salt=unb64u("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    assert body[:86] == unb64u("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8")
    assert body[86:] == unb64u("8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ")


def test_every_push_is_encrypted_fresh():
    device = ec.generate_private_key(ec.SECP256R1())
    first, second = (encrypt(b"x", public_bytes(device), b"a" * 16) for _ in range(2))
    assert first[:16] != second[:16], "the salt repeats"
    assert first[21:86] != second[21:86], "the server key repeats"


def test_the_vapid_token_is_es256_with_the_fixed_sub_and_verifies():
    key = ec.generate_private_key(ec.SECP256R1())
    token = vapid_token("https://fcm.googleapis.com", "https://github.com/cebertowicz/corsia", key, now=1_000_000)
    head, body, signature = token.split(".")
    assert json.loads(unb64u(head)) == {"typ": "JWT", "alg": "ES256"}
    assert json.loads(unb64u(body)) == {"aud": "https://fcm.googleapis.com", "exp": 1_043_200, "sub": "https://github.com/cebertowicz/corsia"}
    raw = unb64u(signature)
    assert len(raw) == 64
    key.public_key().verify(
        encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()),
    )
