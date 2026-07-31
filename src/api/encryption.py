"""
Modern encryption algorithms for /encrypt and /decrypt.

This is the authenticated/keyed-crypto counterpart to ciphers.py's classic
ciphers: everything here is real, non-toy encryption, backed by the
`cryptography` package rather than hand-rolled math. It mirrors ciphers.py's
registry shape (a dict + encrypt_text()/decrypt_text() entry points that
return (result, key_actually_used)) so the command layer can share the same
patterns already established there.

The registry spans a deliberately wide range of algorithms -- from current
industry-standard AEAD ciphers down through legacy/broken ones -- so every
entry also carries a short "security" rating string. That rating is baked
into each /encrypt and /decrypt dropdown option (see ENCRYPTION_CHOICES at
the bottom) so users can see at a glance how trustworthy an algorithm is
*before* they pick it, without needing to already know the crypto history
behind "IDEA" or "RC4". Legacy/weak ciphers are included for compatibility,
education, and CTF/puzzle use -- not because they're recommended. AES-256-GCM
or ChaCha20-Poly1305 are the right default choice for anything that matters.

Every symmetric algorithm here is passphrase-based: the passphrase is never
used as a raw key directly, it's run through PBKDF2-HMAC-SHA256 (200,000
iterations) with a fresh random salt on every encryption. That salt (and
the nonce/IV) travel with the ciphertext -- baked into the single Base64
blob this module hands back -- so decrypting only ever needs the same
passphrase, never anything else to keep track of.

    "none"                 no key is used at all (only OTP has no meaningful
                           "none" case -- kept for shape-parity with ciphers.py)
    "required_or_generate"  every algorithm here -- a key/passphrase is
                           accepted but not required; leaving it blank on
                           encrypt generates a strong random one, which is
                           always required (in full) to decrypt

Security notes surfaced to users belong in each entry's "note" field, not
buried here -- see the registry at the bottom of this file.
"""

import base64
import os
import secrets
from typing import Any, Callable, Dict, List, Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.asymmetric import ec, x25519
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
try:
    # Blowfish/TripleDES/CAST5/IDEA/RC2/ARC4 live here in newer `cryptography`
    # releases and no longer emit a deprecation warning on import from this
    # location.
    from cryptography.hazmat.decrepit.ciphers import algorithms as decrepit_algorithms
except ImportError:  # pragma: no cover -- older `cryptography` versions
    decrepit_algorithms = algorithms
from cryptography.hazmat.primitives.ciphers.aead import (
    AESGCM,
    AESCCM,
    AESGCMSIV,
    AESOCB3,
    AESSIV,
    ChaCha20Poly1305,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

# =========================================================================
# Shared passphrase-derivation helper
# =========================================================================

PBKDF2_ITERATIONS = 200_000


def _derive_key(passphrase: str, salt: bytes, length: int) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=length, salt=salt, iterations=PBKDF2_ITERATIONS)
    return kdf.derive(passphrase.encode("utf-8"))


def _generate_passphrase() -> str:
    return secrets.token_urlsafe(24)


EncryptFn = Callable[[str, str], str]
DecryptFn = Callable[[str, str], str]

# =========================================================================
# Generic factory: 12-byte-nonce AEAD ciphers (authenticated -- tampering or
# a wrong key/passphrase is always detected rather than silently returning
# garbage). Blob layout: 16-byte PBKDF2 salt + 12-byte nonce + ciphertext.
# Covers AES-GCM, ChaCha20-Poly1305, AES-GCM-SIV, AES-CCM, and AES-OCB3.
# =========================================================================

def _make_aead_pair(aead_cls, key_len: int, label: str) -> Tuple[EncryptFn, DecryptFn]:
    def _enc(text: str, key: str) -> str:
        salt = os.urandom(16)
        nonce = os.urandom(12)
        derived = _derive_key(key, salt, key_len)
        ct = aead_cls(derived).encrypt(nonce, text.encode("utf-8"), None)
        return base64.b64encode(salt + nonce + ct).decode("ascii")

    def _dec(text: str, key: str) -> str:
        try:
            blob = base64.b64decode(text.strip(), validate=True)
        except Exception:
            raise ValueError(f"Not a valid Base64 {label} ciphertext blob.")
        if len(blob) < 16 + 12:
            raise ValueError(f"That ciphertext is too short to be a valid {label} blob (expected a 16-byte salt + 12-byte nonce + data).")
        salt, nonce, ct = blob[:16], blob[16:28], blob[28:]
        derived = _derive_key(key, salt, key_len)
        try:
            pt = aead_cls(derived).decrypt(nonce, ct, None)
        except InvalidTag:
            raise ValueError("Decryption failed -- wrong passphrase, or the ciphertext was tampered with/corrupted.")
        return pt.decode("utf-8")

    return _enc, _dec


_encrypt_aes, _decrypt_aes = _make_aead_pair(AESGCM, 32, "AES-GCM")
_encrypt_chacha20, _decrypt_chacha20 = _make_aead_pair(ChaCha20Poly1305, 32, "ChaCha20-Poly1305")
_encrypt_aesgcmsiv, _decrypt_aesgcmsiv = _make_aead_pair(AESGCMSIV, 32, "AES-GCM-SIV")
_encrypt_aesccm, _decrypt_aesccm = _make_aead_pair(AESCCM, 32, "AES-CCM")
_encrypt_aesocb3, _decrypt_aesocb3 = _make_aead_pair(AESOCB3, 32, "AES-OCB3")


# =========================================================================
# AES-256-SIV (deterministic authenticated encryption, RFC 5297 -- no
# nonce at all, so the *same* passphrase + text always produces the *same*
# ciphertext. Needs a double-length key, hence 64 bytes derived below.)
# =========================================================================

def _encrypt_aessiv(text: str, key: str) -> str:
    salt = os.urandom(16)
    derived = _derive_key(key, salt, 64)
    ct = AESSIV(derived).encrypt(text.encode("utf-8"), None)
    return base64.b64encode(salt + ct).decode("ascii")


def _decrypt_aessiv(text: str, key: str) -> str:
    try:
        blob = base64.b64decode(text.strip(), validate=True)
    except Exception:
        raise ValueError("Not a valid Base64 AES-SIV ciphertext blob.")
    if len(blob) < 16 + 16:
        raise ValueError("That ciphertext is too short to be a valid AES-SIV blob (expected a 16-byte salt + data).")
    salt, ct = blob[:16], blob[16:]
    derived = _derive_key(key, salt, 64)
    try:
        pt = AESSIV(derived).decrypt(ct, None)
    except InvalidTag:
        raise ValueError("Decryption failed -- wrong passphrase, or the ciphertext was tampered with/corrupted.")
    return pt.decode("utf-8")


# =========================================================================
# Generic factory: CBC-mode block ciphers with PKCS7 padding and *no* built-
# in authentication (unlike the AEAD ciphers above, a wrong key here doesn't
# reliably raise cleanly -- it depends on padding happening to still be
# valid). Blob layout: 16-byte PBKDF2 salt + IV (one block) + ciphertext.
# Covers Blowfish, Triple DES, Camellia, SM4, SEED, CAST5, IDEA, and RC2.
# =========================================================================

def _make_cbc_pair(cipher_cls, key_len: int, block_bits: int, label: str) -> Tuple[EncryptFn, DecryptFn]:
    block_bytes = block_bits // 8

    def _enc(text: str, key: str) -> str:
        salt = os.urandom(16)
        iv = os.urandom(block_bytes)
        derived = _derive_key(key, salt, key_len)
        padder = padding.PKCS7(block_bits).padder()
        padded = padder.update(text.encode("utf-8")) + padder.finalize()
        encryptor = Cipher(cipher_cls(derived), modes.CBC(iv)).encryptor()
        ct = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(salt + iv + ct).decode("ascii")

    def _dec(text: str, key: str) -> str:
        try:
            blob = base64.b64decode(text.strip(), validate=True)
        except Exception:
            raise ValueError(f"Not a valid Base64 {label} ciphertext blob.")
        if len(blob) < 16 + block_bytes or (len(blob) - 16 - block_bytes) % block_bytes != 0:
            raise ValueError(f"That ciphertext is too short/malformed to be a valid {label} blob (expected a 16-byte salt + {block_bytes}-byte IV + data).")
        salt, iv, ct = blob[:16], blob[16:16 + block_bytes], blob[16 + block_bytes:]
        derived = _derive_key(key, salt, key_len)
        try:
            decryptor = Cipher(cipher_cls(derived), modes.CBC(iv)).decryptor()
            padded = decryptor.update(ct) + decryptor.finalize()
            unpadder = padding.PKCS7(block_bits).unpadder()
            return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
        except Exception:
            raise ValueError("Decryption failed -- wrong passphrase, or the ciphertext was tampered with/corrupted.")

    return _enc, _dec


_encrypt_blowfish, _decrypt_blowfish = _make_cbc_pair(decrepit_algorithms.Blowfish, 16, 64, "Blowfish")
_encrypt_tripledes, _decrypt_tripledes = _make_cbc_pair(decrepit_algorithms.TripleDES, 24, 64, "Triple DES")
_encrypt_camellia, _decrypt_camellia = _make_cbc_pair(algorithms.Camellia, 32, 128, "Camellia")
_encrypt_sm4, _decrypt_sm4 = _make_cbc_pair(algorithms.SM4, 16, 128, "SM4")
_encrypt_seed, _decrypt_seed = _make_cbc_pair(algorithms.SEED, 16, 128, "SEED")
_encrypt_cast5, _decrypt_cast5 = _make_cbc_pair(decrepit_algorithms.CAST5, 16, 64, "CAST5")
_encrypt_idea, _decrypt_idea = _make_cbc_pair(decrepit_algorithms.IDEA, 16, 64, "IDEA")
_encrypt_rc2, _decrypt_rc2 = _make_cbc_pair(decrepit_algorithms.RC2, 16, 64, "RC2")


# =========================================================================
# RC4 / ARC4 (stream cipher -- no block, no IV/padding concept, and no
# authentication whatsoever. A fresh key is derived from a fresh random
# salt on every encryption to avoid RC4's classic keystream-reuse pitfalls,
# but the cipher's own decades of statistical-bias attacks remain -- this
# is here for legacy/educational use, not anything that needs to stay secret.)
# =========================================================================

def _encrypt_rc4(text: str, key: str) -> str:
    salt = os.urandom(16)
    derived = _derive_key(key, salt, 32)
    encryptor = Cipher(decrepit_algorithms.ARC4(derived), mode=None).encryptor()
    ct = encryptor.update(text.encode("utf-8")) + encryptor.finalize()
    return base64.b64encode(salt + ct).decode("ascii")


def _decrypt_rc4(text: str, key: str) -> str:
    try:
        blob = base64.b64decode(text.strip(), validate=True)
    except Exception:
        raise ValueError("Not a valid Base64 RC4 ciphertext blob.")
    if len(blob) < 16:
        raise ValueError("That ciphertext is too short to be a valid RC4 blob (expected a 16-byte salt + data).")
    salt, ct = blob[:16], blob[16:]
    derived = _derive_key(key, salt, 32)
    decryptor = Cipher(decrepit_algorithms.ARC4(derived), mode=None).decryptor()
    pt = decryptor.update(ct) + decryptor.finalize()
    try:
        return pt.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Decryption failed -- wrong passphrase, or the ciphertext is corrupted (RC4 has no built-in integrity check, so a wrong key just produces garbage bytes).")


# =========================================================================
# ECC (ECIES over SECP256R1 -- the "key" is a hex-encoded private scalar,
# acting as an identity: encrypting "to" a key means "only that private key
# can decrypt it". Leaving the key blank generates a fresh keypair and
# encrypts to it, mirroring Simple Substitution's required_or_generate).
# =========================================================================

_ECC_CURVE = ec.SECP256R1()


def _generate_ecc_key() -> str:
    private_key = ec.generate_private_key(_ECC_CURVE)
    value = private_key.private_numbers().private_value
    return format(value, "064x")


def _parse_ecc_private_key(key: str) -> ec.EllipticCurvePrivateKey:
    cleaned = str(key).strip().lower().removeprefix("0x")
    if not cleaned or not all(c in "0123456789abcdef" for c in cleaned):
        raise ValueError("ECC's key must be a hex-encoded private key, e.g. the 64-hex-character value this bot generates for you.")
    try:
        value = int(cleaned, 16)
        return ec.derive_private_key(value, _ECC_CURVE)
    except ValueError:
        raise ValueError("That hex value isn't a valid private key for the SECP256R1 curve.")


def _encrypt_ecc(text: str, key: str) -> str:
    recipient_private = _parse_ecc_private_key(key)
    recipient_public = recipient_private.public_key()

    ephemeral_private = ec.generate_private_key(_ECC_CURVE)
    shared_secret = ephemeral_private.exchange(ec.ECDH(), recipient_public)
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"ecies-encrypt").derive(shared_secret)

    nonce = os.urandom(12)
    ct = AESGCM(derived).encrypt(nonce, text.encode("utf-8"), None)

    ephemeral_public_bytes = ephemeral_private.public_key().public_bytes(
        encoding=Encoding.X962,
        format=PublicFormat.CompressedPoint,
    )
    return base64.b64encode(ephemeral_public_bytes + nonce + ct).decode("ascii")


def _decrypt_ecc(text: str, key: str) -> str:
    recipient_private = _parse_ecc_private_key(key)
    try:
        blob = base64.b64decode(text.strip(), validate=True)
    except Exception:
        raise ValueError("Not a valid Base64 ECC ciphertext blob.")
    if len(blob) < 33 + 12:
        raise ValueError("That ciphertext is too short to be a valid ECIES blob (expected a 33-byte compressed public key + 12-byte nonce + data).")
    ephemeral_public_bytes, nonce, ct = blob[:33], blob[33:45], blob[45:]
    try:
        ephemeral_public = ec.EllipticCurvePublicKey.from_encoded_point(_ECC_CURVE, ephemeral_public_bytes)
    except ValueError:
        raise ValueError("That ciphertext's embedded public key isn't valid for the SECP256R1 curve.")

    shared_secret = recipient_private.exchange(ec.ECDH(), ephemeral_public)
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"ecies-encrypt").derive(shared_secret)
    try:
        pt = AESGCM(derived).decrypt(nonce, ct, None)
    except InvalidTag:
        raise ValueError("Decryption failed -- this isn't the private key this was encrypted to, or the ciphertext was tampered with/corrupted.")
    return pt.decode("utf-8")


# =========================================================================
# ECIES over X25519 (Curve25519) -- the same ECIES shape as SECP256R1 above,
# but with the key-agreement curve behind Signal, WhatsApp, and WireGuard.
# Paired with ChaCha20-Poly1305 rather than AES-GCM, mirroring how these two
# primitives are commonly combined in real-world protocols (e.g. WireGuard).
# =========================================================================

def _generate_x25519_key() -> str:
    private_key = x25519.X25519PrivateKey.generate()
    raw = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return raw.hex()


def _parse_x25519_private_key(key: str) -> x25519.X25519PrivateKey:
    cleaned = str(key).strip().lower().removeprefix("0x")
    if len(cleaned) != 64 or not all(c in "0123456789abcdef" for c in cleaned):
        raise ValueError("ECIES (X25519)'s key must be a 64-hex-character private key, e.g. the value this bot generates for you.")
    try:
        return x25519.X25519PrivateKey.from_private_bytes(bytes.fromhex(cleaned))
    except Exception:
        raise ValueError("That hex value isn't a valid X25519 private key.")


def _encrypt_x25519(text: str, key: str) -> str:
    recipient_private = _parse_x25519_private_key(key)
    recipient_public = recipient_private.public_key()

    ephemeral_private = x25519.X25519PrivateKey.generate()
    shared_secret = ephemeral_private.exchange(recipient_public)
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"x25519-ecies-encrypt").derive(shared_secret)

    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(derived).encrypt(nonce, text.encode("utf-8"), None)

    ephemeral_public_bytes = ephemeral_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(ephemeral_public_bytes + nonce + ct).decode("ascii")


def _decrypt_x25519(text: str, key: str) -> str:
    recipient_private = _parse_x25519_private_key(key)
    try:
        blob = base64.b64decode(text.strip(), validate=True)
    except Exception:
        raise ValueError("Not a valid Base64 ECIES (X25519) ciphertext blob.")
    if len(blob) < 32 + 12:
        raise ValueError("That ciphertext is too short to be a valid ECIES (X25519) blob (expected a 32-byte public key + 12-byte nonce + data).")
    ephemeral_public_bytes, nonce, ct = blob[:32], blob[32:44], blob[44:]
    try:
        ephemeral_public = x25519.X25519PublicKey.from_public_bytes(ephemeral_public_bytes)
    except Exception:
        raise ValueError("That ciphertext's embedded public key isn't a valid X25519 point.")

    shared_secret = recipient_private.exchange(ephemeral_public)
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"x25519-ecies-encrypt").derive(shared_secret)
    try:
        pt = ChaCha20Poly1305(derived).decrypt(nonce, ct, None)
    except InvalidTag:
        raise ValueError("Decryption failed -- this isn't the private key this was encrypted to, or the ciphertext was tampered with/corrupted.")
    return pt.decode("utf-8")


# =========================================================================
# Fernet (AES-128-CBC + HMAC-SHA256 + timestamp) -- a ready-made
# authenticated scheme straight from the `cryptography` library itself.
# Arbitrary passphrases are turned into valid Fernet keys via the same
# PBKDF2 derivation used everywhere else here, so it fits the same
# passphrase-based UX as every other algorithm in this file.
# =========================================================================

def _encrypt_fernet(text: str, key: str) -> str:
    salt = os.urandom(16)
    derived = _derive_key(key, salt, 32)
    fernet_key = base64.urlsafe_b64encode(derived)
    token = Fernet(fernet_key).encrypt(text.encode("utf-8"))
    return base64.b64encode(salt + token).decode("ascii")


def _decrypt_fernet(text: str, key: str) -> str:
    try:
        blob = base64.b64decode(text.strip(), validate=True)
    except Exception:
        raise ValueError("Not a valid Base64 Fernet ciphertext blob.")
    if len(blob) < 16:
        raise ValueError("That ciphertext is too short to be a valid Fernet blob (expected a 16-byte salt + token).")
    salt, token = blob[:16], blob[16:]
    derived = _derive_key(key, salt, 32)
    fernet_key = base64.urlsafe_b64encode(derived)
    try:
        pt = Fernet(fernet_key).decrypt(token)
    except InvalidToken:
        raise ValueError("Decryption failed -- wrong passphrase, or the ciphertext was tampered with/corrupted.")
    return pt.decode("utf-8")


# =========================================================================
# One-Time Pad (true OTP -- the key is random bytes exactly as long as the
# message, XORed byte-for-byte. Perfectly secret if the key is truly
# random, kept secret, and never reused -- this bot can't enforce the
# "never reused" part, so treat any given key as burned after one use.)
# =========================================================================

def _generate_otp_key(plaintext_byte_length: int) -> str:
    return base64.b64encode(os.urandom(plaintext_byte_length)).decode("ascii")


def _encrypt_otp(text: str, key: str) -> str:
    data = text.encode("utf-8")
    try:
        key_bytes = base64.b64decode(key.strip(), validate=True)
    except Exception:
        raise ValueError("OTP's key must be Base64 -- leave it blank to have one generated for you.")
    if len(key_bytes) < len(data):
        raise ValueError(
            f"OTP's key must be at least as long as the message once encoded as UTF-8 bytes "
            f"({len(data)} bytes needed, got {len(key_bytes)}). Leave the key blank to have a "
            "correctly-sized random one generated for you."
        )
    key_bytes = key_bytes[:len(data)]
    ct = bytes(d ^ k for d, k in zip(data, key_bytes))
    return base64.b64encode(ct).decode("ascii")


def _decrypt_otp(text: str, key: str) -> str:
    try:
        ct = base64.b64decode(text.strip(), validate=True)
    except Exception:
        raise ValueError("Not a valid Base64 OTP ciphertext.")
    try:
        key_bytes = base64.b64decode(key.strip(), validate=True)
    except Exception:
        raise ValueError("OTP's key must be the exact Base64 key that was used to encrypt this message.")
    if len(key_bytes) < len(ct):
        raise ValueError(f"This key is too short for this ciphertext ({len(ct)} bytes needed, got {len(key_bytes)}) -- it isn't the key this was encrypted with.")
    key_bytes = key_bytes[:len(ct)]
    data = bytes(c ^ k for c, k in zip(ct, key_bytes))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Decryption produced invalid UTF-8 -- this almost certainly isn't the right key for this ciphertext.")


# =========================================================================
# Registry
# =========================================================================

_PASSPHRASE_HINT = "A passphrase of your choice. Leave blank to have a strong random one generated for you (you must save it to decrypt)."

ENCRYPTION_ALGORITHMS: Dict[str, Dict[str, Any]] = {
    "aes": {
        "name": "AES-256-GCM",
        "security": "Industry standard, very high security",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_aes,
        "decrypt": _decrypt_aes,
        "generate_key": _generate_passphrase,
        "note": "Authenticated encryption -- a wrong passphrase or tampered ciphertext fails cleanly instead of returning garbage. NIST-approved and the backbone of TLS 1.3; the safe default choice.",
    },
    "chacha20": {
        "name": "ChaCha20-Poly1305",
        "security": "Modern IETF/TLS standard, very high security",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_chacha20,
        "decrypt": _decrypt_chacha20,
        "generate_key": _generate_passphrase,
        "note": "Authenticated encryption -- a wrong passphrase or tampered ciphertext fails cleanly instead of returning garbage. A modern AES-GCM alternative, often faster on hardware without AES acceleration.",
    },
    "aesgcmsiv": {
        "name": "AES-256-GCM-SIV",
        "security": "High security, nonce-misuse-resistant (RFC 8452)",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_aesgcmsiv,
        "decrypt": _decrypt_aesgcmsiv,
        "generate_key": _generate_passphrase,
        "note": "Authenticated encryption that stays safe even if a nonce is ever accidentally reused -- a robustness guarantee most AEAD modes (including plain AES-GCM) don't have.",
    },
    "aesccm": {
        "name": "AES-256-CCM",
        "security": "High security, Wi-Fi/Bluetooth standard (RFC 3610)",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_aesccm,
        "decrypt": _decrypt_aesccm,
        "generate_key": _generate_passphrase,
        "note": "Authenticated encryption widely deployed in constrained environments -- it's the cipher behind WPA2's CCMP, Bluetooth LE, and Zigbee.",
    },
    "aesocb3": {
        "name": "AES-256-OCB3",
        "security": "High security, fast, less common in practice",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_aesocb3,
        "decrypt": _decrypt_aesocb3,
        "generate_key": _generate_passphrase,
        "note": "Authenticated encryption that's often faster than GCM in software -- historical patents limited adoption for years, but those have since expired.",
    },
    "aessiv": {
        "name": "AES-256-SIV",
        "security": "High security, deterministic (RFC 5297)",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_aessiv,
        "decrypt": _decrypt_aessiv,
        "generate_key": _generate_passphrase,
        "note": "Deterministic authenticated encryption -- no random nonce, so the same passphrase and text always produce the same ciphertext. Useful when you need that; identical inputs are visibly identical in ciphertext form.",
    },
    "camellia": {
        "name": "Camellia-256-CBC",
        "security": "High security, ISO/NESSIE-recommended, less common",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_camellia,
        "decrypt": _decrypt_camellia,
        "generate_key": _generate_passphrase,
        "note": "A 128-bit-block cipher considered as strong as AES and standardized internationally (ISO, NESSIE, CRYPTREC). This CBC mode has no built-in integrity check, unlike the AEAD ciphers above.",
    },
    "sm4": {
        "name": "SM4-CBC",
        "security": "Moderate security, China's national standard (GB/T)",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_sm4,
        "decrypt": _decrypt_sm4,
        "generate_key": _generate_passphrase,
        "note": "China's national block-cipher standard, mandated in Chinese WAPI/TLS deployments and standardized internationally as ISO/IEC 18033-3:2021. This CBC mode has no built-in integrity check.",
    },
    "seed": {
        "name": "SEED-CBC",
        "security": "Moderate security, Korea's national standard",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_seed,
        "decrypt": _decrypt_seed,
        "generate_key": _generate_passphrase,
        "note": "South Korea's national block-cipher standard (KS X 1213, ISO/IEC 18033-3), historically mandated for Korean e-commerce and banking. This CBC mode has no built-in integrity check.",
    },
    "cast5": {
        "name": "CAST5-CBC",
        "security": "Weak, legacy 64-bit block (old OpenPGP default)",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_cast5,
        "decrypt": _decrypt_cast5,
        "generate_key": _generate_passphrase,
        "note": "A 1990s cipher once used as OpenPGP's default -- its 64-bit block size makes it a poor fit for encrypting meaningful volumes of data today. This CBC mode has no built-in integrity check.",
    },
    "idea": {
        "name": "IDEA-CBC",
        "security": "Weak, legacy 64-bit block (old PGP 2.0 cipher)",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_idea,
        "decrypt": _decrypt_idea,
        "generate_key": _generate_passphrase,
        "note": "Best known as the cipher behind PGP 2.0 in 1991 -- its small block size and known weaknesses in reduced-round variants make it obsolete today. This CBC mode has no built-in integrity check.",
    },
    "rc2": {
        "name": "RC2-CBC",
        "security": "Weak, obsolete 1987 block cipher",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_rc2,
        "decrypt": _decrypt_rc2,
        "generate_key": _generate_passphrase,
        "note": "A 1987 RSA Security cipher once used in early S/MIME and legacy Microsoft software -- long superseded and not recommended for anything today. This CBC mode has no built-in integrity check.",
    },
    "rc4": {
        "name": "RC4 (ARC4)",
        "security": "Insecure, broken, formally deprecated (RFC 7465)",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_rc4,
        "decrypt": _decrypt_rc4,
        "generate_key": _generate_passphrase,
        "note": "A once-ubiquitous stream cipher (SSL/TLS, WEP) with well-documented statistical biases -- the IETF formally prohibited its use in TLS. Included for legacy/educational purposes only.",
    },
    "blowfish": {
        "name": "Blowfish-CBC",
        "security": "Weak by modern standards, legacy 64-bit block",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_blowfish,
        "decrypt": _decrypt_blowfish,
        "generate_key": _generate_passphrase,
        "note": "A legacy 64-bit-block cipher, included for compatibility -- prefer AES-256-GCM or ChaCha20-Poly1305 for anything that actually needs to stay secret.",
    },
    "tripledes": {
        "name": "Triple DES-CBC",
        "security": "Weak, formally deprecated by NIST (2023)",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_tripledes,
        "decrypt": _decrypt_tripledes,
        "generate_key": _generate_passphrase,
        "note": "A legacy 64-bit-block cipher, included for compatibility -- prefer AES-256-GCM or ChaCha20-Poly1305 for anything that actually needs to stay secret.",
    },
    "ecc": {
        "name": "ECC/ECIES (P-256)",
        "security": "Industry standard, high security (NIST curve)",
        "key_hint": "A hex-encoded private key. Leave blank to generate a fresh keypair and encrypt to it (you must save the private key to decrypt).",
        "encrypt": _encrypt_ecc,
        "decrypt": _decrypt_ecc,
        "generate_key": _generate_ecc_key,
        "note": "Elliptic-curve (SECP256R1) public-key encryption via ECIES -- the key acts as an identity: only the matching private key can decrypt what was encrypted to it.",
    },
    "x25519": {
        "name": "ECIES (X25519)",
        "security": "Modern standard, very high security (Curve25519)",
        "key_hint": "A hex-encoded private key. Leave blank to generate a fresh keypair and encrypt to it (you must save the private key to decrypt).",
        "encrypt": _encrypt_x25519,
        "decrypt": _decrypt_x25519,
        "generate_key": _generate_x25519_key,
        "note": "ECIES over Curve25519/X25519 -- the key-agreement curve behind Signal, WhatsApp, and WireGuard, paired here with ChaCha20-Poly1305. Widely regarded as simpler to implement safely than NIST curves.",
    },
    "fernet": {
        "name": "Fernet (AES-128-CBC+HMAC)",
        "security": "Solid security, widely used in practice",
        "key_hint": _PASSPHRASE_HINT,
        "encrypt": _encrypt_fernet,
        "decrypt": _decrypt_fernet,
        "generate_key": _generate_passphrase,
        "note": "A ready-made authenticated scheme from Python's `cryptography` library (AES-128-CBC + HMAC-SHA256 + timestamp) -- battle-tested and used throughout the Python ecosystem.",
    },
    "otpcipher": {
        "name": "One-Time Pad",
        "security": "Perfect secrecy in theory, impractical in practice",
        "key_hint": "A Base64 key at least as long as the message. Leave blank to generate a correctly-sized random one (you must save it to decrypt, and never reuse it).",
        "encrypt": _encrypt_otp,
        "decrypt": _decrypt_otp,
        "generate_key_needs_text": True,
        "generate_key": _generate_otp_key,
        "note": "Perfectly secret if the key is truly random, kept secret, and used exactly once -- reusing an OTP key across messages breaks that guarantee completely.",
    },
}

ENCRYPTION_CHOICES: List[Tuple[str, str]] = [
    (f"{v['name']} \u2014 {v['security']}", key) for key, v in ENCRYPTION_ALGORITHMS.items()
]


def _get_entry(algorithm_key: str) -> Dict[str, Any]:
    entry = ENCRYPTION_ALGORITHMS.get(algorithm_key)
    if entry is None:
        raise ValueError(f"'{algorithm_key}' isn't a supported encryption algorithm.")
    return entry


def encrypt_text(algorithm_key: str, text: str, key: Optional[str]) -> Tuple[str, str]:
    """
    Encrypts `text` with the named algorithm. `key` may be None/blank --
    every algorithm here generates a strong random one on the fly if so.
    Returns (result, key_actually_used); the key is always meaningful here
    (unlike ciphers.py, nothing in this module is truly keyless).

    Raises ValueError if the algorithm key isn't recognized, or the
    supplied key/text is invalid for that algorithm.
    """
    entry = _get_entry(algorithm_key)
    if key and key.strip():
        used_key = key.strip()
    elif entry.get("generate_key_needs_text"):
        used_key = entry["generate_key"](len(text.encode("utf-8")))
    else:
        used_key = entry["generate_key"]()
    return entry["encrypt"](text, used_key), used_key


def decrypt_text(algorithm_key: str, text: str, key: Optional[str]) -> str:
    """
    Decrypts `text` with the named algorithm. `key` must be exactly the
    key/passphrase that was used to encrypt it -- there's no identify-style
    guessing for real encryption, unlike classic ciphers' Identify.
    """
    entry = _get_entry(algorithm_key)
    if not key or not key.strip():
        raise ValueError(f"{entry['name']} needs the exact key/passphrase that was used to encrypt this. {entry['key_hint']}")
    return entry["decrypt"](text, key.strip())
