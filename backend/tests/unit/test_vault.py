"""任务 4.1 凭证保险库的单元测试 + PBT。

覆盖维度（对应任务 4.1 验收点）
---------------------------------
1. encrypt/decrypt round-trip：含空串、ASCII、Unicode、1MB 大串
2. nonce 随机性：同明文两次加密密文不同
3. 主密钥校验：31/33/0 字节、settings.VAULT_MASTER_KEY 非 hex / 缺失
4. 解密失败：错误密钥 / 截断 / 篡改 / < 12 字节
5. hash_access_key：确定性 + 区分性 + 64 hex 格式
6. repr/str 不泄露密钥
7. PBT（**Validates: Requirements 2** / Property 5）：
   - 固定测试 master_key，任意 plaintext (≤500) round-trip 一致
   - 任意 plaintext + 任意 32 字节 master_key round-trip
   - 加密产物不含 plaintext.encode('utf-8') 子串（plaintext min_size=8 避免巧合）
   - len(encrypted) >= len(plaintext.encode('utf-8')) + 12 + 16

所有用例**不依赖数据库 / 网络 / 文件系统**，纯函数测试。
"""

from __future__ import annotations

import os
import re

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.config import get_settings
from app.core.vault import (
    CredentialVault,
    DecryptionError,
    InvalidMasterKeyError,
)

# ---------------------------------------------------------------------------
# Helper：在用例间共享一个固定（确定性）32 字节主密钥
# ---------------------------------------------------------------------------
# 用确定性密钥而不是 os.urandom(32)，是为了让用例失败时可重现；
# 并不会影响 nonce 随机性（每次 encrypt 内部都生成新 nonce）。
TEST_MASTER_KEY = bytes(range(32))  # 0x00..0x1f，正好 32 字节


@pytest.fixture()
def vault() -> CredentialVault:
    """共享 vault 实例，跳过环境变量依赖。"""
    return CredentialVault(master_key=TEST_MASTER_KEY)


# ---------------------------------------------------------------------------
# 1. encrypt / decrypt round-trip
# ---------------------------------------------------------------------------
class TestRoundTrip:
    """加密 → 解密必须恢复原文，覆盖典型与极端长度。"""

    @pytest.mark.parametrize(
        "plaintext",
        [
            "",  # 空串：密文长度 == 12 + 0 + 16 == 28
            "a",
            "hello-world",
            "x" * 1024,  # 1KB
            "中文测试🚀混合 ASCII",  # UTF-8 多字节 + emoji
            "line1\nline2\ttab\x00null",  # 控制字符
        ],
    )
    def test_round_trip(self, vault: CredentialVault, plaintext: str) -> None:
        encrypted = vault.encrypt(plaintext)
        assert isinstance(encrypted, bytes)
        assert vault.decrypt(encrypted) == plaintext

    def test_round_trip_1mb(self, vault: CredentialVault) -> None:
        """1MB 明文：覆盖大数据路径；密文长度严格满足 N + 12 + 16。"""
        plaintext = "A" * (1024 * 1024)
        encrypted = vault.encrypt(plaintext)
        assert len(encrypted) == len(plaintext.encode("utf-8")) + 12 + 16
        assert vault.decrypt(encrypted) == plaintext


# ---------------------------------------------------------------------------
# 2. nonce 随机性
# ---------------------------------------------------------------------------
class TestNonceRandomness:
    """Req 2 设计：同明文两次加密 SHALL 产生不同密文（nonce 必须每次随机）。"""

    def test_same_plaintext_yields_different_ciphertext(
        self, vault: CredentialVault
    ) -> None:
        plaintext = "repeat-me-please"
        c1 = vault.encrypt(plaintext)
        c2 = vault.encrypt(plaintext)
        assert c1 != c2
        # 但两者解密后必须都还原为同一原文
        assert vault.decrypt(c1) == plaintext
        assert vault.decrypt(c2) == plaintext

    def test_nonce_prefix_differs(self, vault: CredentialVault) -> None:
        """前 12 字节是 nonce，本身就应该不同。"""
        c1 = vault.encrypt("x")
        c2 = vault.encrypt("x")
        assert c1[:12] != c2[:12]


# ---------------------------------------------------------------------------
# 3. 主密钥合法性
# ---------------------------------------------------------------------------
class TestMasterKeyValidation:
    """主密钥必须是恰好 32 字节，非法长度 / 缺失 / 非 hex 都应抛 InvalidMasterKeyError。"""

    @pytest.mark.parametrize("length", [0, 1, 16, 31, 33, 64])
    def test_invalid_length_raises(self, length: int) -> None:
        with pytest.raises(InvalidMasterKeyError) as exc_info:
            CredentialVault(master_key=b"\x00" * length)
        # InvalidMasterKeyError 必须是 ValueError 子类（接口稳定性）
        assert isinstance(exc_info.value, ValueError)

    def test_non_bytes_raises(self) -> None:
        # str / 内存视图等非 bytes 输入应明确拒绝
        with pytest.raises(InvalidMasterKeyError):
            CredentialVault(master_key="not-bytes")  # type: ignore[arg-type]
        with pytest.raises(InvalidMasterKeyError):
            CredentialVault(master_key=12345)  # type: ignore[arg-type]

    def test_bytearray_accepted(self) -> None:
        """``bytearray`` 也应能作为 master_key 被规范化为 bytes。"""
        vault = CredentialVault(master_key=bytearray(TEST_MASTER_KEY))
        assert vault.decrypt(vault.encrypt("ok")) == "ok"

    def test_settings_empty_master_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """settings.VAULT_MASTER_KEY 为空字符串时（默认值）应抛 InvalidMasterKeyError。"""
        monkeypatch.setenv("VAULT_MASTER_KEY", "")
        get_settings.cache_clear()
        try:
            with pytest.raises(InvalidMasterKeyError) as exc_info:
                CredentialVault()  # 不传 master_key → 走 settings 路径
            assert "empty" in str(exc_info.value).lower()
        finally:
            get_settings.cache_clear()

    def test_settings_invalid_hex_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """settings.VAULT_MASTER_KEY 含非 hex 字符 → InvalidMasterKeyError。"""
        # 64 个字符但其中含 'g'（非 hex）
        monkeypatch.setenv("VAULT_MASTER_KEY", "g" * 64)
        get_settings.cache_clear()
        try:
            with pytest.raises(InvalidMasterKeyError) as exc_info:
                CredentialVault()
            assert "hex" in str(exc_info.value).lower()
        finally:
            get_settings.cache_clear()

    def test_settings_wrong_length_hex_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """合法 hex 但解码后长度 != 32（如 62 hex chars → 31 bytes）。"""
        monkeypatch.setenv("VAULT_MASTER_KEY", "ab" * 31)  # 62 hex → 31 bytes
        get_settings.cache_clear()
        try:
            with pytest.raises(InvalidMasterKeyError) as exc_info:
                CredentialVault()
            assert "32" in str(exc_info.value)
        finally:
            get_settings.cache_clear()

    def test_settings_valid_hex_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """64 个合法 hex 字符 → 解码 32 字节 → 正常构造。"""
        monkeypatch.setenv("VAULT_MASTER_KEY", "ab" * 32)  # 64 hex → 32 bytes
        get_settings.cache_clear()
        try:
            vault = CredentialVault()
            assert vault.decrypt(vault.encrypt("ok")) == "ok"
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 4. 解密失败路径
# ---------------------------------------------------------------------------
class TestDecryptionFailure:
    """密文异常 / 主密钥错误 / 输入非法 → 必须抛 DecryptionError（ValueError 子类）。"""

    def test_wrong_master_key_raises(self, vault: CredentialVault) -> None:
        encrypted = vault.encrypt("secret-payload")
        other_vault = CredentialVault(master_key=os.urandom(32))
        with pytest.raises(DecryptionError) as exc_info:
            other_vault.decrypt(encrypted)
        assert isinstance(exc_info.value, ValueError)

    def test_truncated_ciphertext_raises(self, vault: CredentialVault) -> None:
        encrypted = vault.encrypt("hello")
        # 砍掉最后一个字节 → 破坏 GCM tag
        with pytest.raises(DecryptionError):
            vault.decrypt(encrypted[:-1])

    def test_truncated_below_minimum_raises(self, vault: CredentialVault) -> None:
        """小于 nonce 长度（< 12 字节）必须直接拒绝，不能 panic。"""
        with pytest.raises(DecryptionError):
            vault.decrypt(b"")  # 空字节
        with pytest.raises(DecryptionError):
            vault.decrypt(b"\x00" * 11)  # 11 字节
        with pytest.raises(DecryptionError):
            vault.decrypt(b"\x00" * 12)  # 12 字节（仅 nonce，没 tag）

    def test_truncated_below_tag_size_raises(self, vault: CredentialVault) -> None:
        """12 ≤ len < 12 + 16 之间也必须拒绝。"""
        for n in (13, 20, 27):
            with pytest.raises(DecryptionError):
                vault.decrypt(b"\x00" * n)

    def test_tampered_ciphertext_raises(self, vault: CredentialVault) -> None:
        encrypted = bytearray(vault.encrypt("hello"))
        # 翻转密文中段一个 bit（避免落在 nonce 上，验证密文完整性）
        encrypted[15] ^= 0x01
        with pytest.raises(DecryptionError):
            vault.decrypt(bytes(encrypted))

    def test_tampered_nonce_raises(self, vault: CredentialVault) -> None:
        encrypted = bytearray(vault.encrypt("hello"))
        encrypted[0] ^= 0x01  # 改 nonce 第一个字节
        with pytest.raises(DecryptionError):
            vault.decrypt(bytes(encrypted))

    def test_tampered_tag_raises(self, vault: CredentialVault) -> None:
        encrypted = bytearray(vault.encrypt("hello"))
        # 最后 16 字节是 tag，翻转最后一个字节
        encrypted[-1] ^= 0x01
        with pytest.raises(DecryptionError):
            vault.decrypt(bytes(encrypted))

    def test_non_bytes_input_raises(self, vault: CredentialVault) -> None:
        with pytest.raises(DecryptionError):
            vault.decrypt("not-bytes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. hash_access_key
# ---------------------------------------------------------------------------
class TestHashAccessKey:
    """SHA-256 必须确定性 + 区分性 + 64 个小写 hex 字符。"""

    HEX_64 = re.compile(r"^[0-9a-f]{64}$")

    @pytest.mark.parametrize(
        "access_key",
        [
            "ACCESS-KEY-1",
            "",  # 空串也应得到稳定 hash（SHA-256("")）
            "中文混合-key-🔐",
            "x" * 256,
        ],
    )
    def test_format_64_hex(self, access_key: str) -> None:
        h = CredentialVault.hash_access_key(access_key)
        assert isinstance(h, str)
        assert self.HEX_64.match(h), f"not 64 lowercase hex: {h!r}"

    def test_deterministic(self) -> None:
        """相同输入恒返回相同 hash（多次调用、不同实例都一致）。"""
        a = CredentialVault.hash_access_key("ACCESS-KEY-1")
        b = CredentialVault.hash_access_key("ACCESS-KEY-1")
        c = CredentialVault.hash_access_key("ACCESS-KEY-1")
        assert a == b == c

    def test_distinguishing(self) -> None:
        """不同输入产生不同 hash（碰撞概率忽略不计）。"""
        h1 = CredentialVault.hash_access_key("ACCESS-KEY-1")
        h2 = CredentialVault.hash_access_key("ACCESS-KEY-2")
        h3 = CredentialVault.hash_access_key("access-key-1")  # 大小写不同
        assert h1 != h2
        assert h1 != h3
        assert h2 != h3

    def test_known_vector_empty_string(self) -> None:
        """SHA-256("") 标准向量校验：防止实现被替换为非标准 hash。"""
        expected = (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
        assert CredentialVault.hash_access_key("") == expected

    def test_static_method_no_master_key_needed(self) -> None:
        """``hash_access_key`` 是静态方法，不需要构造实例（不依赖主密钥）。"""
        # 直接通过类调用即可
        h = CredentialVault.hash_access_key("test-key")
        assert self.HEX_64.match(h)


# ---------------------------------------------------------------------------
# 6. repr / str 不泄露密钥
# ---------------------------------------------------------------------------
class TestSafeRepr:
    """Req 2 AC7 / Req 15 AC1：日志输出绝不能含 master_key。"""

    # 选一个非常具有"密钥特征"的全 0xAA 模式，便于在 repr/str 中检测残留
    SENTINEL_KEY = b"\xaa" * 32

    def test_repr_does_not_leak(self) -> None:
        vault = CredentialVault(master_key=self.SENTINEL_KEY)
        r = repr(vault)
        assert "redacted" in r.lower()
        # 既不能包含原始字节、也不能包含 hex 化的密钥
        assert self.SENTINEL_KEY.hex() not in r
        assert str(self.SENTINEL_KEY) not in r
        assert "aa" * 32 not in r.lower()

    def test_str_does_not_leak(self) -> None:
        vault = CredentialVault(master_key=self.SENTINEL_KEY)
        s = str(vault)
        assert "redacted" in s.lower()
        assert self.SENTINEL_KEY.hex() not in s
        assert "aa" * 32 not in s.lower()

    def test_format_does_not_leak(self) -> None:
        """f-string 内嵌也走 __format__→__str__ 路径；做兜底校验。"""
        vault = CredentialVault(master_key=self.SENTINEL_KEY)
        msg = f"vault={vault}"
        assert "aa" * 32 not in msg.lower()
        assert self.SENTINEL_KEY.hex() not in msg


# ---------------------------------------------------------------------------
# 7. PBT —— **Validates: Requirements 2** / Property 5（密钥不可逆）
# ---------------------------------------------------------------------------
# Property 5 文本（design.md §Correctness Properties）:
#   "encrypted_secret_key 在没有 VAULT_MASTER_KEY 的情况下不可解密；
#    access_key_hash 不可逆推出 access_key。"
#
# 这里把 Property 5 拆解为四条可机器验证的子性质：
#
#   P5.0（固定密钥 round-trip）：在固定测试 master_key 下，对任意 plaintext，
#                              decrypt(encrypt(plaintext)) == plaintext。
#                              ——专注探索 plaintext 维度的边界（短/长/Unicode/控制字符）。
#
#   P5.1（任意密钥 round-trip）：对任意 32 字节 master_key 与任意 plaintext，
#                              decrypt(encrypt(plaintext)) == plaintext。
#                              ——保证"持有正确主密钥时一定能恢复原文"。
#
#   P5.2（机密性）：对任意非平凡 plaintext（≥ 8 字节，避免巧合子串命中），
#                  encrypt(plaintext) 不应包含 plaintext.encode('utf-8') 作为子串。
#                  ——保证"密文里看不到明文"，即使是部分明文也不行。
#
#   P5.3（长度下界）：len(encrypt(plaintext)) >= len(plaintext.encode('utf-8')) + 12 + 16。
#                    ——结构性约束：必须包含 12 字节 nonce + 16 字节 GCM tag，
#                    否则就不是合法的 AES-GCM 密文，反向也意味着密钥不可逆识别。
# ---------------------------------------------------------------------------


@pytest.mark.pbt
class TestPropertyVaultRoundTripFixedKey:
    """**Validates: Requirements 2**（Property 5：固定密钥 round-trip 一致性）。"""

    @given(plaintext=st.text(max_size=500))
    @settings(
        max_examples=60,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
    )
    def test_round_trip_fixed_key(self, plaintext: str) -> None:
        """**Validates: Requirements 2**

        P5.0（最简形式）：在固定测试主密钥下，对任意 UTF-8 文本（≤ 500 字符），
        ``decrypt(encrypt(x)) == x`` 必须恒成立——这是加密保险库最基本的契约。

        与 :class:`TestPropertyVaultEncryptionRoundTrip` 的区别：
        - 本类**固定 master_key**，专注探索 ``plaintext`` 维度的边界
          （短串、长串、Unicode、控制字符、surrogate pair 边界等）；
        - 后者**同时变化 master_key**，验证"任意合法 32 字节密钥都满足契约"。
        两者覆盖维度互补，组合起来构成 Property 5 的完整 round-trip 证据。
        """
        vault = CredentialVault(master_key=TEST_MASTER_KEY)
        encrypted = vault.encrypt(plaintext)
        assert vault.decrypt(encrypted) == plaintext


@pytest.mark.pbt
class TestPropertyVaultEncryptionRoundTrip:
    """**Validates: Requirements 2**（Property 5：密钥不可逆 / round-trip）。"""

    @given(
        master_key=st.binary(min_size=32, max_size=32),
        plaintext=st.text(min_size=0, max_size=200),
    )
    @settings(
        max_examples=60,
        # 在测试中实例化 AESGCM 不便宜，但 60 例 < 1s；关掉 too-slow 健康检查。
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
    )
    def test_encrypt_then_decrypt_recovers_plaintext(
        self, master_key: bytes, plaintext: str
    ) -> None:
        """**Validates: Requirements 2**

        P5.1：对任意 32 字节主密钥 + 任意 UTF-8 文本，加密后再解密必须还原原文。
        """
        vault = CredentialVault(master_key=master_key)
        encrypted = vault.encrypt(plaintext)
        assert vault.decrypt(encrypted) == plaintext


@pytest.mark.pbt
class TestPropertyVaultCiphertextDoesNotLeakPlaintext:
    """**Validates: Requirements 2**（Property 5：机密性）。"""

    @given(
        master_key=st.binary(min_size=32, max_size=32),
        # min_size=8 是为了避免短串巧合命中：
        # 如果 plaintext 只有 1-2 字节，纯随机密文 14 个字节里出现该子串的概率非可忽略。
        # 8 字节明文在 28+ 字节随机密文中偶然出现的概率 < 2^-56，PBT 可放心断言"不出现"。
        plaintext=st.text(min_size=8, max_size=512),
    )
    @settings(
        max_examples=60,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
    )
    def test_encrypted_does_not_contain_plaintext_bytes(
        self, master_key: bytes, plaintext: str
    ) -> None:
        """**Validates: Requirements 2**

        P5.2：密文不得包含 ``plaintext.encode('utf-8')`` 作为子串
        （即"密文里看不到明文"）。这是 AES-GCM 语义安全性的可见证。
        """
        vault = CredentialVault(master_key=master_key)
        encrypted = vault.encrypt(plaintext)
        assert plaintext.encode("utf-8") not in encrypted


@pytest.mark.pbt
class TestPropertyVaultCiphertextLengthLowerBound:
    """**Validates: Requirements 2**（Property 5：结构下界）。"""

    @given(
        master_key=st.binary(min_size=32, max_size=32),
        plaintext=st.text(min_size=0, max_size=2048),
    )
    @settings(
        max_examples=60,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
    )
    def test_encrypted_length_lower_bound(
        self, master_key: bytes, plaintext: str
    ) -> None:
        """**Validates: Requirements 2**

        P5.3：``len(encrypted) >= len(plaintext.encode('utf-8')) + 12 + 16``。
        AES-GCM 流式加密不扩展明文长度，密文 = nonce(12) + ciphertext(N) + tag(16)。
        这里用 ``>=`` 而非 ``==`` 留出未来格式版本号扩展空间，但当前实现下严格相等。
        """
        vault = CredentialVault(master_key=master_key)
        encrypted = vault.encrypt(plaintext)
        expected_min = len(plaintext.encode("utf-8")) + 12 + 16
        assert len(encrypted) >= expected_min
