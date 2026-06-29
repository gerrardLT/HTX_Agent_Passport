"""凭证保险库（Req 2 / 任务 4.1）。

基于 AES-256-GCM 的对称加密保险库，用于保护 HTX API 凭证（access_key / secret_key）。
设计依据：requirements.md Req 2、design.md「凭证保险库：加密方案」、Property 5（密钥不可逆）。

威胁模型与设计要点
------------------
1. **机密性**：明文（access_key / secret_key）只在内存中短暂存在，
   落盘时一律以 AES-256-GCM 密文形式存储。主密钥从环境变量
   ``VAULT_MASTER_KEY``（64 hex 字符 → 32 字节）加载，绝不入库、绝不入日志。
2. **完整性**：AES-GCM 内置 16 字节 authentication tag，任何对密文 / nonce
   的篡改都会在 :meth:`CredentialVault.decrypt` 中触发 ``InvalidTag`` 异常，
   被本模块统一封装为 :class:`DecryptionError`。
3. **唯一性**：每次加密都使用一个全新的 12 字节随机 ``nonce``（``os.urandom``），
   保证相同明文两次加密得到不同密文，杜绝可链接性 / 重放推断。
4. **不可逆识别**：``access_key`` 只存储 SHA-256 摘要（``access_key_hash``），
   仅用于"重复检测"等等值比较场景；不可被反推回原值（Property 5 第二条）。
5. **不可序列化泄露**：``__repr__`` / ``__str__`` 输出固定字符串，
   绝不包含 ``master_key`` 字节；即使被日志框架误打印也不会泄密
   （Req 2 AC7 / Req 15 AC1）。

存储格式
--------
``encrypt`` 返回的字节串布局是::

    [12 bytes nonce][N bytes ciphertext][16 bytes auth tag]

``decrypt`` 期望同样的布局；少于 12 字节直接拒绝（连 nonce 都凑不齐），
否则交给 AES-GCM 内部一次性校验认证标签 + 解密。

使用示例
--------
.. code-block:: python

    vault = CredentialVault()                       # 从 settings.VAULT_MASTER_KEY 读取
    blob = vault.encrypt("super-secret-api-key")    # 落盘
    plain = vault.decrypt(blob)                     # 取出时还原
    h = vault.hash_access_key("ACCESS-KEY-1")       # 64 hex 用于重复检测

    # 测试场景下可显式注入主密钥，避免依赖环境变量：
    vault = CredentialVault(master_key=os.urandom(32))
"""

from __future__ import annotations

import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

# AES-GCM 协议常量
_NONCE_LEN = 12  # NIST SP 800-38D 推荐：96 位 IV，性能与碰撞率折中
_TAG_LEN = 16  # GCM authentication tag 固定 128 位
_KEY_LEN = 32  # AES-256 主密钥长度


class InvalidMasterKeyError(ValueError):
    """主密钥不合法（长度错误 / 非法 hex / 缺失）。

    继承 ``ValueError`` 让上层既可精确捕获 ``InvalidMasterKeyError``，
    也可宽松捕获 ``ValueError``。
    """


class DecryptionError(ValueError):
    """解密失败的统一异常。

    覆盖以下场景：

    - 密文长度小于 12 字节（连 nonce 都没有）
    - 密文长度小于 12 + 16 字节（缺 auth tag）
    - nonce 被截断 / 篡改
    - 密文被篡改（GCM 认证失败）
    - 用错主密钥（GCM 认证失败）
    - 解密结果非合法 UTF-8（明文必须是字符串）

    继承 ``ValueError``，方便上层做统一 ``except ValueError`` 兜底处理。
    """


def _load_master_key_from_settings() -> bytes:
    """从 ``settings.VAULT_MASTER_KEY``（hex 字符串）解码为 32 字节。

    错误处理路径
    ------------
    - 空字符串 / 缺失：抛 ``InvalidMasterKeyError``
    - 非 hex（含奇数长度、非法字符）：抛 ``InvalidMasterKeyError``
    - 解码后长度 ≠ 32：抛 ``InvalidMasterKeyError``
    """
    settings = get_settings()
    raw = settings.VAULT_MASTER_KEY
    if not raw:
        raise InvalidMasterKeyError(
            "VAULT_MASTER_KEY is empty; expect 64 hex characters (32 bytes)."
        )
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise InvalidMasterKeyError(
            "VAULT_MASTER_KEY is not valid hex (expect 64 hex characters)."
        ) from exc
    if len(key) != _KEY_LEN:
        raise InvalidMasterKeyError(
            f"VAULT_MASTER_KEY must decode to {_KEY_LEN} bytes "
            f"(got {len(key)} bytes from {len(raw)} hex chars)."
        )
    return key


class CredentialVault:
    """AES-256-GCM 凭证保险库。

    Parameters
    ----------
    master_key : bytes | None
        32 字节原始密钥。``None`` 时（默认）从
        :attr:`Settings.VAULT_MASTER_KEY`（64 hex 字符）解码读取，便于生产部署。
        测试场景下可显式传入随机 32 字节主密钥，零环境依赖。

    Raises
    ------
    InvalidMasterKeyError
        - ``master_key`` 长度不是 32 字节
        - ``master_key=None`` 且 ``settings.VAULT_MASTER_KEY`` 缺失 / 非法 hex / 长度不对
    """

    # 显式声明实例属性，便于静态分析工具识别
    __slots__ = ("_aesgcm",)

    def __init__(self, master_key: bytes | None = None) -> None:
        if master_key is None:
            key = _load_master_key_from_settings()
        else:
            if not isinstance(master_key, bytes | bytearray):
                raise InvalidMasterKeyError(
                    f"master_key must be bytes (got {type(master_key).__name__})."
                )
            key = bytes(master_key)
            if len(key) != _KEY_LEN:
                raise InvalidMasterKeyError(
                    f"master_key must be exactly {_KEY_LEN} bytes (got {len(key)})."
                )
        # 用单下划线前缀提示"实例内部状态"；__slots__ 已经保证不会被外部新增属性污染。
        # 注意：cryptography 的 AESGCM 内部会持有原始密钥字节，但它本身不暴露 __repr__
        # 中的密钥内容；本类的 __repr__/__str__ 也覆盖为安全占位串（见下）。
        self._aesgcm = AESGCM(key)

    # ------------------------------------------------------------------ encrypt
    def encrypt(self, plaintext: str) -> bytes:
        """加密一段 UTF-8 文本。

        Parameters
        ----------
        plaintext : str
            待加密明文。允许空字符串（密文仍含 nonce + tag，长度 ≥ 28）。

        Returns
        -------
        bytes
            ``[12 byte nonce][ciphertext][16 byte tag]`` 格式的密文。
            每次调用使用全新随机 nonce，相同明文也会得到不同密文。
        """
        nonce = os.urandom(_NONCE_LEN)
        # AESGCM.encrypt 返回的字节串包含密文 + 16 字节 GCM tag（拼接在尾部）。
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return nonce + ciphertext

    # ------------------------------------------------------------------ decrypt
    def decrypt(self, encrypted: bytes) -> str:
        """解密 :meth:`encrypt` 产生的密文。

        Parameters
        ----------
        encrypted : bytes
            ``[12 byte nonce][ciphertext][16 byte tag]`` 格式的密文。

        Returns
        -------
        str
            原始明文。

        Raises
        ------
        DecryptionError
            统一封装所有解密失败：长度不足、nonce/密文/tag 被篡改、
            主密钥不匹配、解密结果非合法 UTF-8 等。
            原始底层异常通过 ``raise ... from exc`` 链保留，
            便于在调试日志中查看根因（生产日志应裁剪 ``__cause__``）。
        """
        # 防御性长度检查：密文必须至少包含 nonce + 一个 GCM tag。
        # 长度不足时直接拒绝，避免把不合法输入交给 AES-GCM（行为未定义/异常类型不一致）。
        if not isinstance(encrypted, bytes | bytearray):
            raise DecryptionError(
                f"encrypted must be bytes (got {type(encrypted).__name__})."
            )
        if len(encrypted) < _NONCE_LEN + _TAG_LEN:
            raise DecryptionError(
                f"encrypted payload too short: need >= {_NONCE_LEN + _TAG_LEN} bytes "
                f"(got {len(encrypted)})."
            )

        nonce = bytes(encrypted[:_NONCE_LEN])
        ciphertext = bytes(encrypted[_NONCE_LEN:])
        try:
            plaintext_bytes = self._aesgcm.decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            # 主密钥不匹配 / 密文被篡改 / nonce 被篡改 → GCM 认证失败
            raise DecryptionError("authentication failed (wrong key or tampered ciphertext).") from exc
        except Exception as exc:  # noqa: BLE001 - 兜底未知底层异常（如 cryptography 升级后新增类型）
            raise DecryptionError("decryption failed.") from exc

        try:
            return plaintext_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            # 极端场景：用同一把主密钥但用不同代码路径写入了非 UTF-8 字节序列。
            # 视为解密失败（破坏数据完整性约束）。
            raise DecryptionError("plaintext is not valid UTF-8.") from exc

    # ----------------------------------------------------------- hash_access_key
    @staticmethod
    def hash_access_key(access_key: str) -> str:
        """计算 access_key 的 SHA-256 摘要（64 个小写 hex 字符）。

        作为 ``api_credentials.access_key_hash`` 列的值；用于"重复检测"等
        等值比较场景。SHA-256 是单向函数，无法从摘要反推原始 access_key
        （Property 5 第二条）。

        Notes
        -----
        - 同一 access_key 在任意时间、任意进程调用本函数都返回相同结果（确定性）。
        - 不同 access_key 几乎不可能产生相同摘要（碰撞概率 ≈ 2^-256）。
        - 本方法是 ``@staticmethod``：``hash_access_key`` 不依赖主密钥，
          因此测试中不需要构造 ``CredentialVault`` 实例就能用。
        """
        return hashlib.sha256(access_key.encode("utf-8")).hexdigest()

    # --------------------------------------------------------------- safe repr
    # Req 2 AC7 / Req 15 AC1：日志中绝不能出现 master_key。即使开发者粗心地
    # 把 vault 对象塞进 logger.info(...)，也不会暴露任何密钥相关信息。
    def __repr__(self) -> str:
        return "CredentialVault(master_key=<redacted>)"

    def __str__(self) -> str:
        return "CredentialVault(master_key=<redacted>)"


__all__ = [
    "CredentialVault",
    "DecryptionError",
    "InvalidMasterKeyError",
]
