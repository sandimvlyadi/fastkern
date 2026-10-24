"""API key generation/hashing. In the domain layer (stdlib only) because the application
layer needs it, and application code may only depend inward, never on infrastructure.

The plaintext already carries 256 bits of entropy (`secrets.token_urlsafe`),
unlike a human password, so a fast, unsalted digest is an accepted trade-off
here — a leaked hash table doesn't meaningfully help an attacker guess a
256-bit random token either way, so the slow salted hashing used for user
passwords would add cost without a matching security benefit here.
"""

import hashlib
import secrets

# A fixed, recognizable marker so secret scanners (the repo's gitleaks rule, a host's own
# secret scanning) can spot a leaked partner key; the random part follows it.
API_KEY_MARKER = "pak_"
_PLAINTEXT_BYTES = 32
# The marker plus 8 random characters: what an admin sees to tell keys apart.
_PREFIX_LENGTH = len(API_KEY_MARKER) + 8


def generate_api_key() -> tuple[str, str, str]:
    """Returns `(plaintext, key_prefix, key_hash)`. `plaintext` is shown to the
    caller exactly once; only `key_prefix` (safe to display/log) and
    `key_hash` are ever persisted."""
    plaintext = API_KEY_MARKER + secrets.token_urlsafe(_PLAINTEXT_BYTES)
    return plaintext, plaintext[:_PREFIX_LENGTH], hash_api_key(plaintext)


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
