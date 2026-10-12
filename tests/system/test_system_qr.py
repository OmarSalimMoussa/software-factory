"""The QR code a phone scans to open the console or the dashboard: its modules, pinned bit for bit."""

from __future__ import annotations

import hashlib

import pytest

from runtime.system import qr


@pytest.mark.parametrize(
    ("link", "size", "digest"),
    [
        ("https://laptop.example.ts.net:8400/?code=123456", 33,
         "260990c6df30a2267ff8da934d1c050ca1ebed6ab56e96bb0c57d87ae1c9d914"),
        ("https://laptop.example.ts.net:8400/" + "x" * 120, 53,
         "75345fd922b565853bf72095250c753a4119276d33fef6cc34245526d896d9b2"),
    ],
    ids=["short-link", "long-link-with-version-bits"],
)  # fmt: skip
def test_a_link_draws_the_same_modules_every_time(link: str, size: int, digest: str) -> None:
    grid = qr.matrix(link)
    bits = "".join("1" if cell else "0" for row in grid for cell in row)

    assert (len(grid), hashlib.sha256(bits.encode()).hexdigest()) == (size, digest)
