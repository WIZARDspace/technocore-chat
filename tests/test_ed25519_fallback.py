"""Verify scripts/_ed25519_fallback.py against RFC 8032 test vectors,
against `cryptography` (byte-for-byte), and against this repo's own
server-side verifier (src/didkey.py)."""
import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _ed25519_fallback import Ed25519PrivateKey as FallbackKey  # noqa: E402

# RFC 8032 §7.1 — Test Vectors, entries 1 and 2 (secret key, public key, message, signature)
# Straight from RFC 8032 https://www.rfc-editor.org/rfc/rfc8032.txt Section 7.1,
# Test 1 and Test 2, with the RFC's line-wrapped hex concatenated back together.
RFC8032_VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc4" "4449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a" "0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a" "84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46b" "d25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f" "5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc" "9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540" "a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c" "387b2eaeb4302aeeb00d291612bb0c00",
    ),
]


def test_rfc8032_vectors():
    for seed_hex, pub_hex, msg_hex, sig_hex in RFC8032_VECTORS:
        seed = bytes.fromhex(seed_hex)
        message = bytes.fromhex(msg_hex)
        key = FallbackKey.from_private_bytes(seed)
        assert key.public_key().public_bytes_raw().hex() == pub_hex, "public key mismatch"
        assert key.sign(message).hex() == sig_hex, "signature mismatch"
    print("PASS: RFC 8032 test vectors 1-2")


def test_matches_cryptography_library():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey as RealKey,
        )
    except BaseException:
        print("SKIP: cryptography not importable in this environment")
        return

    import secrets

    for _ in range(20):
        seed = secrets.token_bytes(32)
        message = secrets.token_bytes(secrets.randbelow(200))
        real = RealKey.from_private_bytes(seed)
        fake = FallbackKey.from_private_bytes(seed)
        assert (
            real.public_key().public_bytes_raw() == fake.public_key().public_bytes_raw()
        ), "public key differs from cryptography"
        assert real.sign(message) == fake.sign(message), "signature differs from cryptography"
    print("PASS: 20 random cases match `cryptography` byte-for-byte")


def test_verifies_against_server_didkey():
    import didkey  # noqa: E402

    def multibase_encode(raw: bytes) -> str:
        b58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        n = int.from_bytes(raw, "big")
        out = ""
        while n:
            n, rem = divmod(n, 58)
            out = b58[rem] + out
        return out

    import secrets

    seed = secrets.token_bytes(32)
    key = FallbackKey.from_private_bytes(seed)
    pub_raw = key.public_key().public_bytes_raw()
    did = "did:key:z" + multibase_encode(b"\xed\x01" + pub_raw)

    message = "lobby|1|hello from the fallback signer"
    sig_raw = key.sign(message.encode("utf-8"))
    sig_b64url = base64.urlsafe_b64encode(sig_raw).decode().rstrip("=")

    didkey.verify(did, sig_b64url, message)  # raises on failure
    print("PASS: server's own didkey.verify() accepts a fallback-signed message")

    try:
        didkey.verify(did, sig_b64url, message + " tampered")
        raise AssertionError("tampered message should have been rejected")
    except didkey.SignatureError:
        print("PASS: server's own didkey.verify() rejects a tampered message")


if __name__ == "__main__":
    test_rfc8032_vectors()
    test_matches_cryptography_library()
    test_verifies_against_server_didkey()
    print("\nAll tests passed.")
