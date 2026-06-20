"""KEK Provider 抽象（修复 G7/G8：主密钥从环境变量迁向 KMS 的可插拔层）。

背景
----
调研（docs/tech-research/02-audit-and-secrets.md）指出：主密钥存环境变量 + app
层单层 AES 加密是业界判定的 "custodial by default" 反模式。生产级做法是
**envelope encryption（信封加密）**：

    KEK（Key Encryption Key，永不离开 HSM/KMS 边界）
      └── 加密每条记录独立的 DEK（Data Encryption Key）
            └── DEK 加密真正的明文（access_key / secret_key）

本模块定义 :class:`KeyProvider` 抽象——只负责 **wrap/unwrap DEK**（即 KEK 操作），
不接触业务明文。这样：

- 本地开发 / 测试：用 :class:`LocalKeyProvider`，KEK 从 ``VAULT_MASTER_KEY``
  派生，零外部依赖，行为与旧 CredentialVault 等价。
- 生产：换成 :class:`AwsKmsKeyProvider`（或未来的 Vault/GCP provider），KEK 永不
  离开 KMS/HSM 边界，wrap/unwrap 通过 KMS API 完成。

切换只是配置开关（``VAULT_KEY_PROVIDER`` 环境变量），业务代码无感知。

key_version
-----------
每个 provider 暴露 ``key_version``（KEK 版本号），写入信封头部。轮换 KEK 时
版本号 +1，旧密文仍可用旧版本 KEK 解开（envelope_vault.rewrap 负责渐进 re-wrap）。
"""

from __future__ import annotations

import abc
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

_NONCE_LEN = 12
_TAG_LEN = 16
_KEK_LEN = 32  # AES-256 KEK


class KeyProviderError(RuntimeError):
    """KEK provider 操作失败（wrap/unwrap/配置错误）。"""


class KeyProvider(abc.ABC):
    """KEK 提供者抽象。

    实现类只做两件事：用 KEK **包裹（wrap）** 一个 DEK，以及 **解包（unwrap）**
    回 DEK。绝不接触业务明文（access_key / secret_key）。
    """

    #: provider 标识，写入信封头部（用于解密时选择正确的 provider）。
    provider_id: str = "base"

    @property
    @abc.abstractmethod
    def key_version(self) -> int:
        """当前 KEK 版本号（轮换时递增）。"""

    @abc.abstractmethod
    def wrap_dek(self, dek: bytes) -> bytes:
        """用 KEK 加密（包裹）一个 DEK，返回 wrapped DEK 字节串。"""

    @abc.abstractmethod
    def unwrap_dek(self, wrapped: bytes, *, key_version: int) -> bytes:
        """用指定版本的 KEK 解开 wrapped DEK，返回明文 DEK。

        Parameters
        ----------
        wrapped : bytes
            :meth:`wrap_dek` 产生的包裹密钥。
        key_version : int
            包裹时使用的 KEK 版本（从信封头部读取）。provider 据此选择
            正确版本的 KEK（支持轮换期间新旧版本并存）。
        """


class LocalKeyProvider(KeyProvider):
    """本地 KEK provider：KEK 从 ``VAULT_MASTER_KEY`` 读取。

    用于开发 / 测试 / 无 KMS 的部署。KEK 即 32 字节主密钥，wrap/unwrap 用
    AES-256-GCM 在进程内完成。

    安全边界（务必知晓）
    --------------------
    KEK 存在于进程内存 + 环境变量，**不具备 HSM/KMS 的隔离性**。这是开发级
    方案；生产请用 :class:`AwsKmsKeyProvider`。本类存在的价值是让 envelope
    encryption 的**格式与轮换机制**在本地就能跑通、可测试，迁移到 KMS 时
    只换 provider、密文格式不变。

    密钥版本与轮换
    --------------
    支持多版本 KEK：``VAULT_MASTER_KEY``（version=当前）+ 可选的
    ``VAULT_MASTER_KEY_V{n}`` 历史版本，让 rewrap 能解开旧密文。
    """

    provider_id = "local"

    __slots__ = ("_keks", "_current_version")

    def __init__(
        self,
        *,
        master_key: bytes | None = None,
        current_version: int = 1,
        historical_keks: dict[int, bytes] | None = None,
    ) -> None:
        """构造本地 provider。

        Parameters
        ----------
        master_key : bytes | None
            当前版本 KEK（32 字节）。None 时从 ``VAULT_MASTER_KEY`` 派生。
        current_version : int
            当前 KEK 版本号。
        historical_keks : dict[int, bytes] | None
            历史版本 KEK 映射（version → 32 字节）；用于轮换期间解开旧密文。
        """
        keks: dict[int, bytes] = dict(historical_keks or {})
        if master_key is None:
            master_key = self._load_master_key()
        master_key = self._validate_kek(master_key)
        keks[current_version] = master_key
        self._keks = keks
        self._current_version = current_version

    @staticmethod
    def _validate_kek(key: bytes) -> bytes:
        if not isinstance(key, (bytes, bytearray)):
            raise KeyProviderError(
                f"KEK must be bytes (got {type(key).__name__})."
            )
        key = bytes(key)
        if len(key) != _KEK_LEN:
            raise KeyProviderError(
                f"KEK must be exactly {_KEK_LEN} bytes (got {len(key)})."
            )
        return key

    @staticmethod
    def _load_master_key() -> bytes:
        settings = get_settings()
        raw = settings.VAULT_MASTER_KEY
        if not raw:
            raise KeyProviderError(
                "VAULT_MASTER_KEY is empty; expect 64 hex characters (32 bytes)."
            )
        try:
            key = bytes.fromhex(raw)
        except ValueError as exc:
            raise KeyProviderError(
                "VAULT_MASTER_KEY is not valid hex (expect 64 hex characters)."
            ) from exc
        if len(key) != _KEK_LEN:
            raise KeyProviderError(
                f"VAULT_MASTER_KEY must decode to {_KEK_LEN} bytes (got {len(key)})."
            )
        return key

    @property
    def key_version(self) -> int:
        return self._current_version

    def wrap_dek(self, dek: bytes) -> bytes:
        kek = self._keks[self._current_version]
        nonce = os.urandom(_NONCE_LEN)
        ciphertext = AESGCM(kek).encrypt(nonce, dek, None)
        return nonce + ciphertext

    def unwrap_dek(self, wrapped: bytes, *, key_version: int) -> bytes:
        kek = self._keks.get(key_version)
        if kek is None:
            raise KeyProviderError(
                f"no KEK available for version {key_version} "
                f"(have versions: {sorted(self._keks)})."
            )
        if len(wrapped) < _NONCE_LEN + _TAG_LEN:
            raise KeyProviderError("wrapped DEK too short.")
        nonce = wrapped[:_NONCE_LEN]
        ciphertext = wrapped[_NONCE_LEN:]
        try:
            return AESGCM(kek).decrypt(nonce, ciphertext, None)
        except Exception as exc:  # noqa: BLE001
            raise KeyProviderError("failed to unwrap DEK (wrong KEK or tampered).") from exc


class AwsKmsKeyProvider(KeyProvider):
    """AWS KMS KEK provider（生产级，需 boto3 + KMS key）。

    用 KMS 的 ``GenerateDataKey`` / ``Decrypt`` 实现 envelope encryption：
    - wrap：调用方传入的 DEK 由 KMS ``Encrypt``（或本 provider 改为让 KMS
      生成 DEK，二选一；这里采用"调用方生成 DEK，KMS 包裹"以与 LocalKeyProvider
      接口一致）。
    - unwrap：KMS ``Decrypt`` wrapped DEK。

    KEK（CMK）永不离开 KMS/HSM 边界（FIPS 140-3 L3）。轮换通过 KMS 自动密钥
    轮换或多 CMK 别名实现。

    实现状态
    --------
    本类是**生产接入点的占位实现**：要启用需安装 boto3、配置 ``VAULT_KMS_KEY_ID``
    与 AWS 凭证（建议用 IAM role / IRSA，而非长期 access key）。当前直接抛
    :class:`NotImplementedError` 并给出接入指引，避免在无 KMS 的环境误以为已启用。
    """

    provider_id = "aws-kms"

    def __init__(self, *, kms_key_id: str | None = None) -> None:
        self._kms_key_id = kms_key_id or get_settings().__dict__.get("VAULT_KMS_KEY_ID", "")
        raise NotImplementedError(
            "AwsKmsKeyProvider 尚未接入：请安装 boto3、配置 VAULT_KMS_KEY_ID 与 "
            "AWS 凭证（推荐 IAM role），并实现 wrap_dek/unwrap_dek 调用 KMS "
            "GenerateDataKey/Decrypt。当前生产部署前请完成此项（见 "
            "docs/tech-research/02-audit-and-secrets.md G7/G8）。"
        )

    @property
    def key_version(self) -> int:  # pragma: no cover - 占位
        raise NotImplementedError

    def wrap_dek(self, dek: bytes) -> bytes:  # pragma: no cover - 占位
        raise NotImplementedError

    def unwrap_dek(self, wrapped: bytes, *, key_version: int) -> bytes:  # pragma: no cover
        raise NotImplementedError


def get_key_provider() -> KeyProvider:
    """按配置（``VAULT_KEY_PROVIDER``）构造 KEK provider。

    - ``"local"``（默认）→ :class:`LocalKeyProvider`
    - ``"aws-kms"`` → :class:`AwsKmsKeyProvider`

    生产部署应设 ``VAULT_KEY_PROVIDER=aws-kms``。
    """
    settings = get_settings()
    provider_name = getattr(settings, "VAULT_KEY_PROVIDER", "local") or "local"
    if provider_name == "local":
        return LocalKeyProvider()
    if provider_name == "aws-kms":
        return AwsKmsKeyProvider()
    raise KeyProviderError(f"unknown VAULT_KEY_PROVIDER: {provider_name!r}")


__all__ = [
    "AwsKmsKeyProvider",
    "KeyProvider",
    "KeyProviderError",
    "LocalKeyProvider",
    "get_key_provider",
]
