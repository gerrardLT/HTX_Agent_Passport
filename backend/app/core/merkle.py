"""RFC 6962 风格 Merkle Hash Tree（修复 G10/G11）。

背景
----
我们的审计哈希链是单向链表（``event_hash = sha256(canonical + prev + ts)``），
能检测「篡改单事件后不重算」的攻击，但**无法防御**「整段删除后用新 prev 重算
全链」（详见 docs/tech-research/02-audit-and-secrets.md G10/G11）。

修复方式：在线性链之上**追加** Merkle 树层 + 周期签名的 Tree Head（STH）。
- 任何事件被删除/插入/篡改 → root hash 变化 → 与已对外发布的 STH 不一致 → 被检测。
- O(log N) inclusion proof：评委可在不下载全链的前提下证明"某事件确实在日志中"。
- 与线性链共存：双重防御，``verify_chain_integrity`` 仍然有效。

实现选择：RFC 6962
------------------
采用 IETF RFC 6962（Certificate Transparency v1）的 Merkle 树定义：

- 叶子哈希：``H(0x00 || leaf_data)``
- 节点哈希：``H(0x01 || left || right)``
- 空树哈希：``H()`` （SHA-256 of empty input）
- 不平衡树支持任意 N，无需补齐到 2 的幂

这是最广泛部署的可验证日志标准（Google CT、Trillian、各类二进制透明性日志），
跨语言库齐全（Go/Rust/Python/Java），便于第三方审计工具集成。

本模块仅含纯函数，无 I/O、无 DB 依赖；DB 层与 STH 签名见 audit_merkle_service.py。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Final

# RFC 6962 §2.1 leaf/node domain separation prefixes.
_LEAF_PREFIX: Final[bytes] = b"\x00"
_NODE_PREFIX: Final[bytes] = b"\x01"

#: Hex 长度（SHA-256 → 64 字符）。
HASH_HEX_LEN: Final[int] = 64


def _h(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _hex(data: bytes) -> str:
    return data.hex()


def _unhex(data: str) -> bytes:
    if len(data) != HASH_HEX_LEN:
        raise ValueError(f"hash hex must be {HASH_HEX_LEN} chars, got {len(data)}")
    return bytes.fromhex(data)


def leaf_hash(leaf_data: bytes) -> str:
    """RFC 6962 叶子哈希：``H(0x00 || leaf_data)``，返回 hex。

    Parameters
    ----------
    leaf_data : bytes
        要纳入树的数据。在本项目中 = ``audit_event.event_hash`` 的 hex 解码字节
        （见 :func:`event_hash_to_leaf`）——这样既保留了原线性链的密码学
        承诺，又把"事件标识"作为 Merkle 树的叶子。

    Returns
    -------
    str
        64 字符小写 hex 的叶子哈希。
    """
    return _hex(_h(_LEAF_PREFIX + leaf_data))


def node_hash(left_hex: str, right_hex: str) -> str:
    """RFC 6962 内部节点哈希：``H(0x01 || left || right)``，返回 hex。"""
    return _hex(_h(_NODE_PREFIX + _unhex(left_hex) + _unhex(right_hex)))


def event_hash_to_leaf(event_hash_hex: str) -> str:
    """把现有 audit_events.event_hash（线性链 hash）转换为 Merkle 叶子哈希。

    我们不重新选 leaf data，而是直接用线性链的 ``event_hash``（hex 字符串解码
    成 32 字节）作为 leaf input。这样：
    - Merkle 树承诺的内容 = 线性链已经承诺过的（双重防御，无内容歧义）
    - 任何事件 ``event_json`` 的篡改既会破坏线性链又会破坏 Merkle 树
    """
    return leaf_hash(_unhex(event_hash_hex))


def merkle_root(leaves_hex: Sequence[str]) -> str:
    """计算 ``leaves`` 列表的 Merkle 根（RFC 6962 §2.1）。

    递归定义（不平衡时左多右少）::

        MTH({})              = H()
        MTH({d})             = leaf_hash(d)（注意：调用方应已传入 leaf_hash 后的 hex）
        MTH(D[0:n]) (n>1)    = node_hash( MTH(D[0:k]), MTH(D[k:n]) )
                               where k = 最大的不超过 n 的 2 的幂

    注意本函数的输入是**已经 leaf_hash 后的 hex 列表**——避免重复哈希。
    叶子层的 leaf_hash 由调用方（service 层）批量完成。

    Parameters
    ----------
    leaves_hex : Sequence[str]
        叶子哈希的 hex 字符串列表（顺序敏感，按事件时间顺序）。

    Returns
    -------
    str
        根哈希 hex（64 字符）；空列表返回空树哈希 ``sha256(b'').hex()``。
    """
    n = len(leaves_hex)
    if n == 0:
        return _hex(_h(b""))
    if n == 1:
        return leaves_hex[0]
    k = _largest_power_of_two_below(n)
    left = merkle_root(leaves_hex[:k])
    right = merkle_root(leaves_hex[k:])
    return node_hash(left, right)


def _largest_power_of_two_below(n: int) -> int:
    """RFC 6962 §2.1 splitter：返回严格小于 n 的最大 2 的幂。

    用于把 n 个叶子分成 [0:k] 和 [k:n] 两段，使左子树是完美二叉树。

    Examples
    --------
    >>> _largest_power_of_two_below(2)
    1
    >>> _largest_power_of_two_below(3)
    2
    >>> _largest_power_of_two_below(7)
    4
    >>> _largest_power_of_two_below(8)
    4
    """
    if n <= 1:
        raise ValueError(f"n must be > 1, got {n}")
    k = 1
    while (k << 1) < n:
        k <<= 1
    return k


def inclusion_proof(leaves_hex: Sequence[str], index: int) -> list[str]:
    """生成索引 ``index`` 处叶子的 inclusion proof（RFC 6962 §2.1.1）。

    Returns
    -------
    list[str]
        从树底向上的兄弟节点 hash 列表（hex）。配合 :func:`verify_inclusion_proof`
        可由根 hash 验证某叶子确实在树中、位于该索引位置。

    Raises
    ------
    IndexError
        ``index`` 越界。
    """
    n = len(leaves_hex)
    if not 0 <= index < n:
        raise IndexError(f"index {index} out of range [0, {n})")
    return _path(leaves_hex, index)


def _path(leaves_hex: Sequence[str], m: int) -> list[str]:
    """RFC 6962 §2.1.1 PATH(m, D[n]) 递归实现。"""
    n = len(leaves_hex)
    if n == 1:
        return []
    k = _largest_power_of_two_below(n)
    if m < k:
        # 目标在左子树；右子树根作为 sibling 加入路径
        return _path(leaves_hex[:k], m) + [merkle_root(leaves_hex[k:])]
    # 目标在右子树；左子树根作为 sibling 加入路径
    return _path(leaves_hex[k:], m - k) + [merkle_root(leaves_hex[:k])]


def verify_inclusion_proof(
    leaf_hex: str,
    index: int,
    tree_size: int,
    proof: Sequence[str],
    expected_root_hex: str,
) -> bool:
    """验证 inclusion proof：用 ``leaf`` + ``proof`` 重算根并与 ``expected_root`` 比对。

    Parameters
    ----------
    leaf_hex : str
        叶子哈希 hex（即 :func:`leaf_hash` 的输出）。
    index : int
        叶子在树中的索引（0-based）。
    tree_size : int
        树的总叶子数（用于解析 proof 路径）。
    proof : Sequence[str]
        :func:`inclusion_proof` 输出的 sibling hash 列表。
    expected_root_hex : str
        期望的根 hash（来自 STH）。

    Returns
    -------
    bool
        True = 证明通过；False = 证明失败（被篡改/不一致）。
    """
    if not 0 <= index < tree_size:
        return False
    if tree_size == 1:
        return len(proof) == 0 and leaf_hex == expected_root_hex

    fn = index  # 我们正在跟踪叶子的（子）树内索引
    sn = tree_size - 1  # 当前子树最大索引
    h = leaf_hex
    for sibling in proof:
        if sn == 0:
            return False
        # 找到当前子树根所在层级的最大 2 的幂分割点
        if fn % 2 == 1 or fn == sn:
            # 当前节点是右孩子（fn 是奇数 或 是该层最右节点且来自右子树）
            h = node_hash(sibling, h)
            if fn % 2 == 0:
                # fn == sn 但 fn 偶数：上溯时持续右移直到出现偶数父亲
                while fn % 2 == 0:
                    fn >>= 1
                    sn >>= 1
        else:
            h = node_hash(h, sibling)
        fn >>= 1
        sn >>= 1
    return h == expected_root_hex


def consistency_proof(
    old_leaves_hex: Sequence[str],
    new_leaves_hex: Sequence[str],
) -> list[str]:
    """生成"旧树是新树前缀"的 consistency proof（RFC 6962 §2.1.2）。

    用于证明：``new_tree`` 是 ``old_tree`` 的 append-only 扩展，
    历史叶子未被改动/删除/重排。

    Returns
    -------
    list[str]
        证明路径（hex 节点哈希列表）；空树或两树相等时返回空列表。

    Raises
    ------
    ValueError
        新树长度小于旧树（违反 append-only 假设）。
    """
    m = len(old_leaves_hex)
    n = len(new_leaves_hex)
    if m > n:
        raise ValueError(f"new tree size {n} smaller than old tree size {m}")
    if m == 0 or m == n:
        return []
    # 旧树必须是新树的严格前缀
    if list(old_leaves_hex) != list(new_leaves_hex[:m]):
        raise ValueError("old tree is not a prefix of new tree (append-only violated)")
    return _subproof(m, new_leaves_hex, True)


def _subproof(m: int, leaves_hex: Sequence[str], b: bool) -> list[str]:
    """RFC 6962 §2.1.2 SUBPROOF(m, D[n], b) 递归实现。"""
    n = len(leaves_hex)
    if m == n:
        if b:
            return []
        return [merkle_root(leaves_hex)]
    if m < n:
        k = _largest_power_of_two_below(n)
        if m <= k:
            return _subproof(m, leaves_hex[:k], b) + [merkle_root(leaves_hex[k:])]
        return _subproof(m - k, leaves_hex[k:], False) + [merkle_root(leaves_hex[:k])]
    raise ValueError("unreachable: m > n already rejected by caller")


__all__ = [
    "HASH_HEX_LEN",
    "consistency_proof",
    "event_hash_to_leaf",
    "inclusion_proof",
    "leaf_hash",
    "merkle_root",
    "node_hash",
    "verify_inclusion_proof",
]
