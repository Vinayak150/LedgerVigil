"""Envelope encryption for provider secrets (ADR-016, Stage 2C §11).

A fresh 256-bit data key encrypts each secret with AES-256-GCM; the data key is wrapped with RSA-OAEP (SHA-256) under the
key-encryption key (KEK). The API process holds only the KEK's PUBLIC key, so it can seal an authorization code but can never
open a stored token; only the integration worker holds the private key. Associated data binds every envelope to its purpose,
tenant, connection, realm and token version, so an envelope copied to another row or replayed from an older version fails.

Layout (version 1): b'LVE1' | kek_id (32 bytes) | wrapped key length (2 bytes, big-endian) | wrapped key | nonce (12) | ciphertext+tag
"""
import hashlib
import os
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b'LVE1'
_OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


class EnvelopeError(Exception):
    """The envelope cannot be opened with this key and associated data (wrong key, tampered bytes, or wrong binding)."""


def kek_id(public_key) -> str:
    der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def load_public(pem: bytes):
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
        raise EnvelopeError('the key-encryption public key must be RSA with at least 3072 bits')
    return key


def load_private(pem: bytes):
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 3072:
        raise EnvelopeError('the key-encryption private key must be RSA with at least 3072 bits')
    return key


def generate_kek():
    """A new 3072-bit key pair as (private PEM, public PEM). Used only by tools/provider_setup.py."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, public


def aad(purpose: str, *parts: str) -> bytes:
    if purpose not in ('authorization-code', 'tokens'):
        raise ValueError('unknown envelope purpose')
    return '|'.join(('lv-provider/v1', purpose, *parts)).encode()


def seal(public_key, plaintext: bytes, associated: bytes) -> bytes:
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    wrapped = public_key.encrypt(data_key, _OAEP)
    body = AESGCM(data_key).encrypt(nonce, plaintext, associated)
    return MAGIC + bytes.fromhex(kek_id(public_key)) + struct.pack('>H', len(wrapped)) + wrapped + nonce + body


def envelope_kek_id(envelope: bytes) -> str:
    if len(envelope) < 64 or envelope[:4] != MAGIC:
        raise EnvelopeError('not a LedgerVigil provider envelope')
    return envelope[4:36].hex()


def open_(private_key, envelope: bytes, associated: bytes) -> bytes:
    if envelope_kek_id(envelope) != kek_id(private_key.public_key()):
        raise EnvelopeError('sealed under a different key-encryption key')
    (length,) = struct.unpack('>H', envelope[36:38])
    wrapped, rest = envelope[38:38 + length], envelope[38 + length:]
    if len(wrapped) != length or len(rest) < 12 + 16:
        raise EnvelopeError('truncated envelope')
    try:
        data_key = private_key.decrypt(wrapped, _OAEP)
        return AESGCM(data_key).decrypt(rest[:12], rest[12:], associated)
    except Exception:
        raise EnvelopeError('envelope authentication failed') from None
