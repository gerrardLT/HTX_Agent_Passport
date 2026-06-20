"""信封加密保险库（修复 G7/G8/G9）。

在 :class:`app.core.key_provider.KeyProvider`（KEK 层）之上实现 envelope
encryption：每条记录生成独立 DEK 加密明文，KEK 包裹 DEK。相比旧的单层
:class:`app.core.vault.CredentialVault`（主密钥直接加密数据），envelope 模式：

- **KEK 可不落地**：生产用 AWS KMS 时 KEK 永不离开 HSM 边界。
- **可轮换**（G9）：轮换 KEK 只需 re-wrap DEK（:func:`rewrap`），不重加密 payload。
- **每记录唯一 DEK**：单个 DEK 泄露不影响其它记录。

信封格式（version 2，magic 前缀区分旧格式）
------------------------------------------
::

    [4 bytes magic "EV02"]
    [1 byte  provider_id_len][provider_id utf-8]
    [2 bytes key_version (big-endian)]
    [2 bytes wrapped_dek_len (big-endian)][wrapped_dek]
    [12 bytes payload_nonce][payload_ciphertext + 16 byte tag]

向后兼容
--------
:meth:`EnvelopeVault.decrypt` 检测到非 "EV02" magic 时，回退到旧的
:class:`CredentialVault` 单层解密路径——保证迁移期已有 DB 行（旧格式密文）
仍可解开。新写入一律用 envelope 格式。
"""

from __future__ import annotations

import os
import struct

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.key_provider import KeyProvider, KeyProviderError, get_key_provider
from app.core.vault import CredentialVault, DecryptionError

_MAGIC = b"EV02"
_NONCE_LEN = 12
_TAG_LEN = 16
_DEK_LEN = 32  # AES-256 DEK


class EnvelopeVault:
    """信封加密保险库。

    Parameters
    ----------
    key_provider : KeyProvider | None
        KEK provider；None 时按配置（``VAULT_KEY_PROVIDER``）构造。
    legacy_vault : CredentialVault | None
        旧格式密文的回退解密器；None 时按需惰性构造（从 VAULT_MASTER_KEY）。
        测试中可注入显式实例。
    """

    __slots__ = ("_kp", "_legacy_vault")

    def __init__(
        self,
        *,
        key_provider: KeyProvider | None = None,
        legacy_vault: CredentialVault | None = None,
    ) -> None:
        self._kp = key_provider if key_provider is not None else get_key_provider()
        self._legacy_vault = legacy_vault

    # ------------------------------------------------------------------ encrypt
    def encrypt(self, plaintext: str) -> bytes:
        """信封加密一段 UTF-8 文本。

        流程：生成随机 DEK → 用 DEK(AES-GCM) 加密明文 → 用 KEK 包裹 DEK →
        组装信封字节串。
        """
        dek = os.urandom(_DEK_LEN)
        payload_nonce = os.urandom(_NONCE_LEN)
        payload_ct = AESGCM(dek).encrypt(payload_nonce, plaintext.encode("utf-8"), None)
        wrapped_dek = self._kp.wrap_dek(dek)

        provider_id = self._kp.provider_id.encode("utf-8")
        key_version = self._kp.key_version

        return (
            _MAGIC
            + struct.pack("!B", len(provider_id))
            + provider_id
            + struct.pack("!H", key_version)
            + struct.pack("!H", len(wrapped_dek))
            + wrapped_dek
            + payload_nonce
            + payload_ct
        )

    # ------------------------------------------------------------------ decrypt
    def decrypt(self, blob: bytes) -> str:
        """解密信封密文；非信封格式回退到旧 CredentialVault 单层解密。"""
        if not isinstance(blob, (bytes, bytearray)):
            raise DecryptionError(
                f"encrypted must be bytes (got {type(blob).__name__})."
            )
        blob = bytes(blob)

        if not blob.startswith(_MAGIC):
            # 向后兼容：旧格式（单层 AES）密文
            return self._legacy_decrypt(blob)

        try:
            offset = len(_MAGIC)
            (pid_len,) = struct.unpack_from("!B", blob, offset)
            offset += 1
            provider_id = blob[offset : offset + pid_len].decode("utf-8")
            offset += pid_len
            (key_version,) = struct.unpack_from("!H", blob, offset)
            offset += 2
            (wrapped_len,) = struct.unpack_from("!H", blob, offset)
            offset += 2
            wrapped_dek = blob[offset : offset + wrapped_len]
            offset += wrapped_len
            payload = blob[offset:]
        except (struct.error, UnicodeDecodeError, IndexError) as exc:
            raise DecryptionError("malformed envelope header.") from exc

        if len(payload) < _NONCE_LEN + _TAG_LEN:
            raise DecryptionError("envelope payload too short.")

        # provider_id 校验：当前 provider 必须匹配信封记录的 provider
        if provider_id != self._kp.provider_id:
            raise DecryptionError(
                f"envelope provider mismatch: blob={provider_id!r} "
                f"current={self._kp.provider_id!r}."
            )

        try:
            dek = self._kp.unwrap_dek(wrapped_dek, key_version=key_version)
        except KeyProviderError as exc:
            raise DecryptionError("failed to unwrap DEK.") from exc

        payload_nonce = payload[:_NONCE_LEN]
        payload_ct = payload[_NONCE_LEN:]
        try:
            plaintext_bytes = AESGCM(dek).decrypt(payload_nonce, payload_ct, None)
        except Exception as exc:  # noqa: BLE001
            raise DecryptionError("payload authentication failed.") from exc

        try:
            return plaintext_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DecryptionError("plaintext is not valid UTF-8.") from exc

    # ------------------------------------------------------------------ rewrap
    def rewrap(self, blob: bytes) -> bytes:
        """密钥轮换：把密文 re-wrap 到当前 KEK 版本（G9）。

        解开 payload 后用**新的 DEK + 当前版本 KEK** 重新封装。仅当信封的
        key_version 落后于当前版本，或来自旧格式时才需要。返回新信封密文。

        Notes
        -----
        这是 Vault Transit "rewrap" 模式的本地等价：轮换 KEK 后，后台 job
        遍历 ``api_credentials`` 的密文列，对每条调用 ``rewrap`` 并回写，
        即可在不停机、不暴露明文给轮换 job 之外的前提下完成密钥轮换。
        （注：本地实现需先 decrypt 再 re-encrypt；KMS 版本可用 KMS ReEncrypt
        做到明文 DEK 不出 KMS。）
        """
        plaintext = self.decrypt(blob)
        return self.encrypt(plaintext)

    def needs_rewrap(self, blob: bytes) -> bool:
        """判断密文是否需要 re-wrap（旧格式或 KEK 版本落后）。"""
        if not isinstance(blob, (bytes, bytearray)):
            return False
        blob = bytes(blob)
        if not blob.startswith(_MAGIC):
            return True  # 旧格式 → 需要迁移到信封格式
        try:
            offset = len(_MAGIC)
            (pid_len,) = struct.unpack_from("!B", blob, offset)
            offset += 1 + pid_len
            (key_version,) = struct.unpack_from("!H", blob, offset)
        except struct.error:
            return False
        return key_version < self._kp.key_version

    # ------------------------------------------------------------------ helpers
    def _legacy_decrypt(self, blob: bytes) -> str:
        if self._legacy_vault is None:
            self._legacy_vault = CredentialVault()
        return self._legacy_vault.decrypt(blob)

    @staticmethod
    def hash_access_key(access_key: str) -> str:
        """SHA-256 摘要（委托给 CredentialVault，保持单一实现）。"""
        return CredentialVault.hash_access_key(access_key)

    def __repr__(self) -> str:
        return f"EnvelopeVault(provider={self._kp.provider_id!r})"

    def __str__(self) -> str:
        return self.__repr__()


__all__ = ["EnvelopeVault"]
