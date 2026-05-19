"""Crypto payload dataclasses — raw bytes, no serialization."""

from dataclasses import dataclass


@dataclass
class EncryptionPayload:
    """Encrypted data — output of encrypt, input of decrypt."""

    cipher_data: bytes
    iv: bytes
    tag: bytes


@dataclass
class DecryptionPayload:
    """Plain data — output of decrypt, input of encrypt."""

    plain_data: bytes
