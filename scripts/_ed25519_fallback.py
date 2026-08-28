"""Pure stdlib Ed25519 (RFC 8032), used only when `cryptography` cannot be
imported — e.g. environments with no working C-extension build (PEP 723's
`uv run` requires a package manager and a working wheel; some sandboxes,
like a-Shell on iOS, have neither).

This implements exactly the reference algorithm specified in RFC 8032 §5.1
(key generation and signing over edwards25519), using only `hashlib` from
the standard library. Ed25519 is deterministic, so for a given seed and
message this produces byte-identical output to any other correct
implementation — verified in tests/test_ed25519_fallback.py against:
  - RFC 8032 §7.1 test vectors 1 and 2
  - direct comparison against `cryptography`'s output for random seeds
  - round-trip verification through this repo's own src/didkey.py (nacl)

Intentionally minimal: this exists to keep the signed lane reachable when
`cryptography` is not installable, not to replace it as the default path.
"""
from __future__ import annotations

import hashlib

_B = 256
_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493


def _H(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _expmod(base: int, e: int, m: int) -> int:
    if e == 0:
        return 1
    t = _expmod(base, e // 2, m) ** 2 % m
    if e & 1:
        t = (t * base) % m
    return t


def _inv(x: int) -> int:
    return _expmod(x, _Q - 2, _Q)


_D = -121665 * _inv(121666) % _Q
_I = _expmod(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = _expmod(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


_BY = 4 * _inv(5)
_BX = _xrecover(_BY)
_BASE = (_BX % _Q, _BY % _Q)


def _edwards(P: tuple[int, int], Q: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _D * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _D * x1 * x2 * y1 * y2)
    return (x3 % _Q, y3 % _Q)


def _scalarmult(P: tuple[int, int], e: int) -> tuple[int, int]:
    if e == 0:
        return (0, 1)
    Qp = _scalarmult(P, e // 2)
    Qp = _edwards(Qp, Qp)
    if e & 1:
        Qp = _edwards(Qp, P)
    return Qp


def _encodeint(y: int) -> bytes:
    bits = [(y >> i) & 1 for i in range(_B)]
    return bytes(
        sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_B // 8)
    )


def _encodepoint(P: tuple[int, int]) -> bytes:
    x, y = P
    bits = [(y >> i) & 1 for i in range(_B - 1)] + [x & 1]
    return bytes(
        sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_B // 8)
    )


def _bit(h: bytes, i: int) -> int:
    return (h[i // 8] >> (i % 8)) & 1


def _clamped_scalar(seed_hash: bytes) -> int:
    return 2 ** (_B - 2) + sum(2**i * _bit(seed_hash, i) for i in range(3, _B - 2))


def _hint(m: bytes) -> int:
    h = _H(m)
    return sum(2**i * _bit(h, i) for i in range(2 * _B))


class Ed25519PublicKey:
    def __init__(self, raw: bytes):
        self._raw = raw

    def public_bytes_raw(self) -> bytes:
        return self._raw


class Ed25519PrivateKey:
    """Drop-in replacement for the two
    `cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey`
    methods sign.py actually uses: `from_private_bytes`, `.sign`, and
    `.public_key().public_bytes_raw()`.
    """

    def __init__(self, seed: bytes):
        if len(seed) != 32:
            raise ValueError("Ed25519 seed must be exactly 32 bytes")
        self._seed = seed
        h = _H(seed)
        a = _clamped_scalar(h)
        self._a = a
        self._prefix = h[_B // 8 : _B // 4]
        self._public_raw = _encodepoint(_scalarmult(_BASE, a))

    @classmethod
    def from_private_bytes(cls, data: bytes) -> "Ed25519PrivateKey":
        return cls(data)

    def public_key(self) -> Ed25519PublicKey:
        return Ed25519PublicKey(self._public_raw)

    def sign(self, message: bytes) -> bytes:
        r = _hint(self._prefix + message)
        R = _scalarmult(_BASE, r)
        S = (r + _hint(_encodepoint(R) + self._public_raw + message) * self._a) % _L
        return _encodepoint(R) + _encodeint(S)
