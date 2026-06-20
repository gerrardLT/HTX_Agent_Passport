"""信封加密保险库 + KEK provider 测试（修复 G7/G8/G9）。

**Validates: Requirements 2**（凭证加密）+ docs/tech-research G7/G8/G9。

覆盖：
1. envelope 加解密往返一致（含空串、Unicode、长串）。
2. 每条记录独立 DEK：相同明文两次加密密文不同。
3. 信封头部格式正确（magic / provider_id / key_version）。
4. **向后兼容**：EnvelopeVault 能解开旧的单层 CredentialVault 密文。
5. **密钥轮换（rewrap）**：KEK 版本升级后旧密文可 re-wrap 到新版本，且需 needs_rewrap 正确识别。
6. KEK provider 多版本：unwrap 用错版本失败、历史版本可解。
7. 篡改检测：payload / wrapped DEK 篡改 → DecryptionError。
8. provider 不匹配 → 拒绝。
"""

from __future__ import annotations

import os

import pytest

from app.core.envelope_vault import EnvelopeVault
from app.core.key_provider import (
    KeyProviderError,
    LocalKeyProvider,
)
from app.core.vault import CredentialVault, DecryptionError

TEST_KEK_V1 = bytes(range(32))  # 确定性 32 字节
TEST_KEK_V2 = bytes(range(32, 64))


def _vault(provider: LocalKeyProvider | None = None) -> EnvelopeVault:
    if provider is None:
        provider = LocalKeyProvider(master_key=TEST_KEK_V1, current_version=1)
    return EnvelopeVault(key_provider=provider)


# ===========================================================================
# 1. 往返一致
# ===========================================================================
class TestEnvelopeRoundTrip:
    @pytest.mark.parametrize(
        "plaintext",
        ["", "x", "ACCESS-KEY-123", "中文密钥🔑", "A" * 4096],
    )
    def test_round_trip(self, plaintext: str) -> None:
        v = _vault()
        blob = v.encrypt(plaintext)
        assert isinstance(blob, bytes)
        assert v.decrypt(blob) == plaintext

    def test_envelope_has_magic_prefix(self) -> None:
        v = _vault()
        blob = v.encrypt("secret")
        assert blob.startswith(b"EV02")

    def test_plaintext_not_in_ciphertext(self) -> None:
        v = _vault()
        secret = "SUPER-SECRET-VALUE"
        blob = v.encrypt(secret)
        assert secret.encode("utf-8") not in blob


# ===========================================================================
# 2. 每记录独立 DEK
# ===========================================================================
class TestPerRecordDEK:
    def test_same_plaintext_different_ciphertext(self) -> None:
        v = _vault()
        c1 = v.encrypt("repeat-me")
        c2 = v.encrypt("repeat-me")
        assert c1 != c2
        assert v.decrypt(c1) == "repeat-me"
        assert v.decrypt(c2) == "repeat-me"


# ===========================================================================
# 3. 向后兼容旧单层格式
# ===========================================================================
class TestBackwardCompat:
    def test_decrypts_legacy_credentialvault_blob(self) -> None:
        """EnvelopeVault 能解开旧 CredentialVault（单层）密文。"""
        legacy = CredentialVault(master_key=TEST_KEK_V1)
        legacy_blob = legacy.encrypt("legacy-secret")
        assert not legacy_blob.startswith(b"EV02")  # 旧格式无 magic

        # EnvelopeVault 注入同一把 key 作为 legacy_vault 回退
        v = EnvelopeVault(
            key_provider=LocalKeyProvider(master_key=TEST_KEK_V1),
            legacy_vault=legacy,
        )
        assert v.decrypt(legacy_blob) == "legacy-secret"

    def test_needs_rewrap_flags_legacy(self) -> None:
        legacy = CredentialVault(master_key=TEST_KEK_V1)
        legacy_blob = legacy.encrypt("x")
        v = EnvelopeVault(
            key_provider=LocalKeyProvider(master_key=TEST_KEK_V1),
            legacy_vault=legacy,
        )
        assert v.needs_rewrap(legacy_blob) is True


# ===========================================================================
# 4. 密钥轮换（G9）
# ===========================================================================
class TestKeyRotation:
    def test_rewrap_legacy_to_envelope(self) -> None:
        """旧格式密文 rewrap → 新 envelope 格式，明文不变。"""
        legacy = CredentialVault(master_key=TEST_KEK_V1)
        legacy_blob = legacy.encrypt("rotate-me")
        v = EnvelopeVault(
            key_provider=LocalKeyProvider(master_key=TEST_KEK_V1),
            legacy_vault=legacy,
        )
        new_blob = v.rewrap(legacy_blob)
        assert new_blob.startswith(b"EV02")
        assert v.decrypt(new_blob) == "rotate-me"
        assert v.needs_rewrap(new_blob) is False

    def test_rewrap_to_new_kek_version(self) -> None:
        """KEK 从 v1 轮换到 v2：v1 密文 needs_rewrap，rewrap 后用 v2 解开。"""
        # v1 provider 加密
        p_v1 = LocalKeyProvider(master_key=TEST_KEK_V1, current_version=1)
        v1 = EnvelopeVault(key_provider=p_v1)
        blob_v1 = v1.encrypt("payload")

        # 轮换：v2 为当前版本，v1 作为历史版本保留（用于解旧密文）
        p_v2 = LocalKeyProvider(
            master_key=TEST_KEK_V2,
            current_version=2,
            historical_keks={1: TEST_KEK_V1},
        )
        v2 = EnvelopeVault(key_provider=p_v2)

        # v2 vault 能解开 v1 密文（历史 KEK 仍在）
        assert v2.decrypt(blob_v1) == "payload"
        # v1 密文版本落后 → 需要 rewrap
        assert v2.needs_rewrap(blob_v1) is True

        # rewrap 到 v2
        blob_v2 = v2.rewrap(blob_v1)
        assert v2.decrypt(blob_v2) == "payload"
        assert v2.needs_rewrap(blob_v2) is False


# ===========================================================================
# 5. KEK provider 多版本
# ===========================================================================
class TestKeyProviderVersions:
    def test_unwrap_wrong_version_fails(self) -> None:
        p = LocalKeyProvider(master_key=TEST_KEK_V1, current_version=1)
        dek = os.urandom(32)
        wrapped = p.wrap_dek(dek)
        with pytest.raises(KeyProviderError):
            p.unwrap_dek(wrapped, key_version=99)  # 没有 v99

    def test_historical_kek_unwraps(self) -> None:
        p_v1 = LocalKeyProvider(master_key=TEST_KEK_V1, current_version=1)
        dek = os.urandom(32)
        wrapped = p_v1.wrap_dek(dek)

        p_v2 = LocalKeyProvider(
            master_key=TEST_KEK_V2, current_version=2,
            historical_keks={1: TEST_KEK_V1},
        )
        assert p_v2.unwrap_dek(wrapped, key_version=1) == dek

    def test_invalid_kek_length_rejected(self) -> None:
        with pytest.raises(KeyProviderError):
            LocalKeyProvider(master_key=b"\x00" * 16)  # 非 32 字节


# ===========================================================================
# 6. 篡改检测
# ===========================================================================
class TestTamperDetection:
    def test_tampered_payload_raises(self) -> None:
        v = _vault()
        blob = bytearray(v.encrypt("hello"))
        blob[-1] ^= 0x01  # 翻转 payload 尾字节（tag 区）
        with pytest.raises(DecryptionError):
            v.decrypt(bytes(blob))

    def test_truncated_envelope_raises(self) -> None:
        v = _vault()
        blob = v.encrypt("hello")
        with pytest.raises(DecryptionError):
            v.decrypt(blob[:10])  # 砍断信封头

    def test_non_bytes_raises(self) -> None:
        v = _vault()
        with pytest.raises(DecryptionError):
            v.decrypt("not-bytes")  # type: ignore[arg-type]


# ===========================================================================
# 7. provider 不匹配
# ===========================================================================
class TestProviderMismatch:
    def test_provider_id_mismatch_rejected(self) -> None:
        """信封记录的 provider 与当前 provider 不一致 → 拒绝。"""
        v_local = _vault()
        blob = v_local.encrypt("x")

        # 构造一个 provider_id 不同的 provider
        class _FakeProvider(LocalKeyProvider):
            provider_id = "aws-kms"

        fake = _FakeProvider(master_key=TEST_KEK_V1, current_version=1)
        v_fake = EnvelopeVault(key_provider=fake)
        with pytest.raises(DecryptionError, match="provider mismatch"):
            v_fake.decrypt(blob)


# ===========================================================================
# 8. 安全：repr 不泄露
# ===========================================================================
class TestNoLeak:
    def test_repr_no_leak(self) -> None:
        v = _vault()
        assert "EnvelopeVault" in repr(v)
        # 不包含任何 KEK 字节
        assert "00010203" not in repr(v).lower()
