"""Pydantic models for the load balancer — HTTP and WS crypto payloads."""

import base64
from typing import Literal

from pydantic import BaseModel

from core.models.crypto import EncryptionPayload


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class EncryptionHTTPRequest(BaseModel):
    """HTTP request with encrypted payload (includes RSA-encrypted AES key)."""

    encrypted_key: str  # base64
    iv: str             # base64
    cipher_data: str    # base64
    tag: str            # base64

    def to_raw_payload(self) -> tuple[EncryptionPayload, bytes]:
        """Convert to raw dataclass payload + encrypted_key bytes."""
        return EncryptionPayload(
            cipher_data=base64.b64decode(self.cipher_data),
            iv=base64.b64decode(self.iv),
            tag=base64.b64decode(self.tag),
        ), base64.b64decode(self.encrypted_key)


class EncryptionHTTPResponse(BaseModel):
    """HTTP response with encrypted payload (AES-only, client already has key)."""

    iv: str             # base64
    cipher_data: str    # base64
    tag: str            # base64

    def to_raw_payload(self) -> EncryptionPayload:
        return EncryptionPayload(
            cipher_data=base64.b64decode(self.cipher_data),
            iv=base64.b64decode(self.iv),
            tag=base64.b64decode(self.tag),
        )

    @staticmethod
    def from_raw_payload(payload: EncryptionPayload) -> "EncryptionHTTPResponse":
        return EncryptionHTTPResponse(
            iv=base64.b64encode(payload.iv).decode(),
            cipher_data=base64.b64encode(payload.cipher_data).decode(),
            tag=base64.b64encode(payload.tag).decode(),
        )


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


class WSKeyExchangeRequest(BaseModel):
    """WS key exchange: client sends RSA-encrypted AES session key."""

    type: Literal["key_exchange"]
    encrypted_key: str  # base64

    def to_raw_key(self) -> bytes:
        return base64.b64decode(self.encrypted_key)


class WSEncryptedMessage(BaseModel):
    """WS encrypted message (after key exchange, both directions)."""

    type: Literal["encrypted"]
    iv: str             # base64
    cipher_data: str    # base64
    tag: str            # base64

    def to_raw_payload(self) -> EncryptionPayload:
        return EncryptionPayload(
            cipher_data=base64.b64decode(self.cipher_data),
            iv=base64.b64decode(self.iv),
            tag=base64.b64decode(self.tag),
        )

    @staticmethod
    def from_raw_payload(payload: EncryptionPayload) -> "WSEncryptedMessage":
        return WSEncryptedMessage(
            type="encrypted",
            iv=base64.b64encode(payload.iv).decode(),
            cipher_data=base64.b64encode(payload.cipher_data).decode(),
            tag=base64.b64encode(payload.tag).decode(),
        )
