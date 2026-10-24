from app.modules.api_clients.domain.hashing import generate_api_key, hash_api_key


def test_generate_api_key_produces_unique_values() -> None:
    plaintext1, prefix1, hash1 = generate_api_key()
    plaintext2, prefix2, hash2 = generate_api_key()
    assert plaintext1 != plaintext2
    assert hash1 != hash2
    assert prefix1 != prefix2


def test_hash_api_key_round_trips() -> None:
    plaintext, _, key_hash = generate_api_key()
    assert hash_api_key(plaintext) == key_hash


def test_key_prefix_is_a_prefix_of_the_plaintext() -> None:
    plaintext, prefix, _ = generate_api_key()
    assert plaintext.startswith(prefix)
