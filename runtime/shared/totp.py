"""Six-digit codes from the owner's authenticator app (RFC 6238, SHA-1, 30-second steps).

The secret is readable by the warden's user alone, so no agent on the laptop can make a code. A code from the step
before or after is accepted, for a phone whose clock drifts. Stdlib only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct

STEP_S = 30
DIGITS = 6


def new_secret() -> str:
    """A fresh base32 secret for an authenticator app."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def code_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def valid(secret: str, code: str, now_s: float) -> bool:
    """Whether ``code`` is the code for now, or for the step just before or after."""
    if len(code) != DIGITS or not code.isdigit():
        return False
    counter = int(now_s // STEP_S)
    return any(
        hmac.compare_digest(code_at(secret, counter + d), code) for d in (-1, 0, 1)
    )


def enrolment_uri(secret: str, account: str) -> str:
    return f"otpauth://totp/software%20factory:{account}?secret={secret}&issuer=software%20factory&digits={DIGITS}&period={STEP_S}"
