"""STH 签名抽象层（Phase 2 生产升级）。

把签名算法从 :mod:`audit_merkle_service` 抽出，支持两种 backend：

- ``HMAC-SHA256``（默认，向后兼容）—— 现有 STH 行的签名格式。
- ``Ed25519``（生产推荐 / RFC 8032）—— 非对称签名，公钥可对外发布让第三方
  审计员 / 评委独立验证，符合 RFC 6962 / C2SP signed-note 标准的"对外可验证"
  transparency log 期望。

设计要点
--------
1. **signature 字段加版本前缀**：``hmac:<hex>`` / ``ed25519:<hex>``。这让数据库
   既有的 HMAC 行（无前缀）和新签发的 Ed25519 行能在同一表共存——读取时按
   前缀路由，未带前缀的视为 HMAC 兼容。
2. **算法选择从配置读取**：``AUDIT_STH_SIGNING_ALGO`` 默认 ``hmac-sha256``,
   生产改 ``ed25519``。验证路径**总是**两路径都尝试——没有"切换日"，新旧 STH
   行可永久共存。
3. **私钥懒加载 + 缓存**：首次签发时从 ``AUDIT_STH_ED25519_PRIVATE_KEY_PATH``
   读私钥文件，之后缓存在模块内。这避免每次签发的 I/O 开销，也避免启动时
   即需要 Ed25519 配置（开发/CI 默认走 HMAC，生产才需配置）。
4. **公钥可独立派生 / 配置**：公钥文件可选；若未配置，从私钥派生即可——
   pyca/cryptography 的 ``Ed25519PrivateKey.public_key()`` 派生稳定。

为什么 Ed25519 而非 ECDSA P-256？
--------------------------------
RFC 8032 + C2SP signed-note v1.0 默认 Ed25519（签名类型 ``0x01``），
理由：64 字节固定签名（ECDSA P-256 是 ~70 字节 DER 变长）、确定性签名
（同密钥 + 同消息 → 恒定签名，便于测试重放）、无 padding/曲线参数协商攻击面。
未来要接入 C2SP 公开 transparency log 网络（witness 共签）走 Ed25519 是直通车。

为什么不一次性删掉 HMAC？
------------------------
1. 现有 ``audit_tree_heads`` 表可能已积累大量 HMAC 签发的行（生产升级时不能
   失效历史 STH 验证）。
2. CI / 单元测试不需要 Ed25519 配置（开发体验）。
3. 让"算法切换"成为运行期配置而非代码迁移。

未来可选升级
------------
- AWS KMS Ed25519 私钥（2023 起 KMS 支持），不落盘只调 ``Sign`` API。
- C2SP signed-note 文本格式输出（4 字节 key ID + base64 签名行）。
- 后量子 Dilithium：在 ``signature`` 字段前缀加 ``dilithium2:`` 引入。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from functools import lru_cache
from pathlib import Path
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from app.core.config import get_settings

logger = logging.getLogger(__name__)


#: 签名 hex 串前缀；存到 ``audit_tree_heads.signature`` 列。
HMAC_PREFIX: Final[str] = "hmac:"
ED25519_PREFIX: Final[str] = "ed25519:"


class STHSigningError(RuntimeError):
    """签名 / 验证 / 配置异常。"""


# ---------------------------------------------------------------------------
# 共享工具：构造 canonical payload
# ---------------------------------------------------------------------------
def _canonical_payload(tree_size: int, root_hash: str, signed_at_iso: str) -> bytes:
    """STH 签名的规范字节串。

    格式严格固定：``v1|{tree_size}|{root_hash}|{signed_at_iso}`` UTF-8 编码。
    把版本号 ``v1`` 放进去给未来格式升级留空间——若改为 v2 必须更新此函数
    + 加新版本前缀。
    """
    return f"v1|{tree_size}|{root_hash}|{signed_at_iso}".encode()


# ---------------------------------------------------------------------------
# HMAC-SHA256 签名（保留，向后兼容）
# ---------------------------------------------------------------------------
def _hmac_signing_key() -> bytes:
    """从配置读取 HMAC 签名密钥；空时回退到 ``JWT_SECRET``。"""
    settings = get_settings()
    key = getattr(settings, "AUDIT_STH_SIGNING_KEY", "") or settings.JWT_SECRET
    if not key:
        raise STHSigningError(
            "no HMAC signing key configured "
            "(set AUDIT_STH_SIGNING_KEY or JWT_SECRET)"
        )
    return key.encode("utf-8")


def _sign_hmac(tree_size: int, root_hash: str, signed_at_iso: str) -> str:
    """HMAC-SHA256 签名，返回带前缀的 hex 串。"""
    payload = _canonical_payload(tree_size, root_hash, signed_at_iso)
    sig = hmac.new(_hmac_signing_key(), payload, hashlib.sha256).hexdigest()
    return f"{HMAC_PREFIX}{sig}"


def _verify_hmac(
    tree_size: int, root_hash: str, signed_at_iso: str, signature_hex: str
) -> bool:
    """常时间比较 HMAC 签名是否匹配。"""
    expected = _sign_hmac(tree_size, root_hash, signed_at_iso)
    return hmac.compare_digest(expected, signature_hex)


# ---------------------------------------------------------------------------
# Ed25519 签名（新增，生产推荐）
# ---------------------------------------------------------------------------
def _load_ed25519_private_key(path: str | Path) -> Ed25519PrivateKey:
    """从 PEM/DER/Raw 32 字节文件加载 Ed25519 私钥。

    支持的格式：
    1. PEM PKCS8（``-----BEGIN PRIVATE KEY-----``，**推荐**）。
    2. DER PKCS8（二进制）。
    3. Raw 32 字节（裸 seed，最小）。

    自动检测：先尝试 PEM → 失败再 DER → 再 Raw。生产建议 PEM PKCS8 + passphrase
    （此函数仅支持无 passphrase 私钥；带 passphrase 的需扩展 ``password=`` 参数）。
    """
    path = Path(path)
    if not path.exists():
        raise STHSigningError(f"Ed25519 private key file not found: {path}")
    data = path.read_bytes()

    # 尝试 PEM PKCS8
    try:
        key = serialization.load_pem_private_key(data, password=None)
        if isinstance(key, Ed25519PrivateKey):
            return key
        raise STHSigningError(
            f"key at {path} is not Ed25519 (got {type(key).__name__})"
        )
    except (ValueError, TypeError):
        pass  # 不是 PEM，继续试 DER

    # 尝试 DER PKCS8
    try:
        key = serialization.load_der_private_key(data, password=None)
        if isinstance(key, Ed25519PrivateKey):
            return key
        raise STHSigningError(
            f"key at {path} is not Ed25519 (got {type(key).__name__})"
        )
    except (ValueError, TypeError):
        pass  # 不是 DER，继续试 Raw

    # Raw 32 字节
    if len(data) == 32:
        return Ed25519PrivateKey.from_private_bytes(data)

    raise STHSigningError(
        f"unrecognized Ed25519 key format at {path}: "
        f"expected PEM PKCS8 / DER PKCS8 / 32-byte raw, got {len(data)} bytes"
    )


def _load_ed25519_public_key_from_path(path: str | Path) -> Ed25519PublicKey:
    """从 PEM/DER/Raw 32 字节文件加载 Ed25519 公钥。"""
    path = Path(path)
    if not path.exists():
        raise STHSigningError(f"Ed25519 public key file not found: {path}")
    data = path.read_bytes()

    try:
        key = serialization.load_pem_public_key(data)
        if isinstance(key, Ed25519PublicKey):
            return key
    except (ValueError, TypeError):
        pass

    try:
        key = serialization.load_der_public_key(data)
        if isinstance(key, Ed25519PublicKey):
            return key
    except (ValueError, TypeError):
        pass

    if len(data) == 32:
        return Ed25519PublicKey.from_public_bytes(data)

    raise STHSigningError(
        f"unrecognized Ed25519 public key format at {path}"
    )


@lru_cache(maxsize=1)
def _get_cached_ed25519_private_key() -> Ed25519PrivateKey:
    """懒加载 + 缓存 Ed25519 私钥。

    设计为模块级 LRU cache（maxsize=1）：私钥文件一旦加载常驻内存到进程结束。
    重新读盘只在 ``settings.AUDIT_STH_ED25519_PRIVATE_KEY_PATH`` 改变时——
    实务中靠重启进程触发。测试 fixture 用 :func:`reset_signing_caches`
    显式清缓存。
    """
    settings = get_settings()
    path = settings.AUDIT_STH_ED25519_PRIVATE_KEY_PATH
    if not path:
        raise STHSigningError(
            "AUDIT_STH_SIGNING_ALGO=ed25519 requires "
            "AUDIT_STH_ED25519_PRIVATE_KEY_PATH to be set"
        )
    return _load_ed25519_private_key(path)


@lru_cache(maxsize=1)
def _get_cached_ed25519_public_key() -> Ed25519PublicKey:
    """获取 Ed25519 公钥：优先公钥文件，否则从私钥派生。"""
    settings = get_settings()
    pub_path = settings.AUDIT_STH_ED25519_PUBLIC_KEY_PATH
    if pub_path:
        return _load_ed25519_public_key_from_path(pub_path)
    return _get_cached_ed25519_private_key().public_key()


def _sign_ed25519(tree_size: int, root_hash: str, signed_at_iso: str) -> str:
    """Ed25519 签名，返回带前缀的 hex 串（128 字符 = 64 字节）。"""
    payload = _canonical_payload(tree_size, root_hash, signed_at_iso)
    private_key = _get_cached_ed25519_private_key()
    sig = private_key.sign(payload)
    return f"{ED25519_PREFIX}{sig.hex()}"


def _verify_ed25519(
    tree_size: int, root_hash: str, signed_at_iso: str, signature_hex: str
) -> bool:
    """Ed25519 验证；签名不匹配 → False（不抛异常）。"""
    if not signature_hex.startswith(ED25519_PREFIX):
        return False
    raw_hex = signature_hex[len(ED25519_PREFIX):]
    try:
        sig_bytes = bytes.fromhex(raw_hex)
    except ValueError:
        return False
    payload = _canonical_payload(tree_size, root_hash, signed_at_iso)
    try:
        public_key = _get_cached_ed25519_public_key()
        public_key.verify(sig_bytes, payload)
        return True
    except InvalidSignature:
        return False
    except STHSigningError:
        # 公钥未配置时无法验证；这是配置错误而非签名错误，向上抛
        raise


# ---------------------------------------------------------------------------
# Public API: sign / verify dispatchers
# ---------------------------------------------------------------------------
def sign_sth(tree_size: int, root_hash: str, signed_at_iso: str) -> str:
    """按 ``settings.AUDIT_STH_SIGNING_ALGO`` 签发 STH 签名。

    Returns
    -------
    str
        带算法前缀的 hex 字符串：``hmac:<64-hex>`` 或 ``ed25519:<128-hex>``。
        调用方直接存入 ``audit_tree_heads.signature``。
    """
    settings = get_settings()
    algo = (settings.AUDIT_STH_SIGNING_ALGO or "hmac-sha256").lower()
    if algo == "hmac-sha256":
        return _sign_hmac(tree_size, root_hash, signed_at_iso)
    if algo == "ed25519":
        return _sign_ed25519(tree_size, root_hash, signed_at_iso)
    raise STHSigningError(
        f"unknown AUDIT_STH_SIGNING_ALGO={algo!r} "
        "(expected 'hmac-sha256' or 'ed25519')"
    )


def verify_sth_signature(
    tree_size: int, root_hash: str, signed_at_iso: str, signature_hex: str
) -> bool:
    """按签名前缀路由到对应验证器。

    向后兼容：无前缀的 hex 视为旧版 HMAC（按 64 字符 hex 长度判断）。

    Notes
    -----
    本函数**不区分**算法切换前后写入的 STH——任何 ``signature`` 列的值都能
    被正确验证（前提是对应密钥仍可访问）。这是支撑"零切换日"运行期算法
    切换的关键。
    """
    if signature_hex.startswith(ED25519_PREFIX):
        return _verify_ed25519(tree_size, root_hash, signed_at_iso, signature_hex)
    if signature_hex.startswith(HMAC_PREFIX):
        return _verify_hmac(tree_size, root_hash, signed_at_iso, signature_hex)
    # 无前缀 = 旧版 HMAC（64 hex chars）；构造带前缀版本再比对
    if len(signature_hex) == 64:
        return _verify_hmac(
            tree_size, root_hash, signed_at_iso, f"{HMAC_PREFIX}{signature_hex}"
        )
    return False


def get_public_key_hex() -> str | None:
    """返回 Ed25519 公钥的 raw hex（32 字节 = 64 字符）；HMAC 模式返回 None。

    供 ``GET /api/audit/sth/verifier-key`` 端点暴露给外部审计员。
    """
    settings = get_settings()
    algo = (settings.AUDIT_STH_SIGNING_ALGO or "hmac-sha256").lower()
    if algo != "ed25519":
        return None
    public_key = _get_cached_ed25519_public_key()
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return raw.hex()


def get_signing_algo() -> str:
    """返回当前签名算法标识（``hmac-sha256`` / ``ed25519``）。"""
    settings = get_settings()
    return (settings.AUDIT_STH_SIGNING_ALGO or "hmac-sha256").lower()


def reset_signing_caches() -> None:
    """清空私钥/公钥缓存。

    测试 fixture 在切换 settings 后必须调用此函数，否则 LRU cache 会让
    新配置不生效。生产代码不应主动调用——靠重启进程刷新。
    """
    _get_cached_ed25519_private_key.cache_clear()
    _get_cached_ed25519_public_key.cache_clear()


__all__ = [
    "ED25519_PREFIX",
    "HMAC_PREFIX",
    "STHSigningError",
    "get_public_key_hex",
    "get_signing_algo",
    "reset_signing_caches",
    "sign_sth",
    "verify_sth_signature",
]
