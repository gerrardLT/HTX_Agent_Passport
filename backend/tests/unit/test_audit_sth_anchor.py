"""STH 外部锚定模块测试（Phase 1 / G10-G11 跟进）。

**Validates: G10/G11 外部锚定**——把 STH 周期写到本地 JSONL 文件，让
"删除 + 全链重写" 攻击窗口被关闭（DBA 改 DB 后无法在不留痕的情况下
回滚锚定文件）。

覆盖：
1. 路径为空字符串 → False，且不视为错误（让调用方无脑传 settings）
2. 正常写入 JSONL 一行（合法 JSON + 含全部 7 个字段）
3. 多次调用追加（不覆盖）
4. 幂等：同一 STH 重复调用不重写
5. 不可写路径（如不存在的盘符 / 锁住目录）吞错并返回 False
6. 父目录不存在时自动创建
7. 损坏的最后一行不让锚定崩溃
8. None passport_id（用户级链）字段写入为 JSON null
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models import AuditTreeHead
from app.services.audit_sth_anchor import anchor_sth_to_file


def _make_sth(
    *,
    user_id: uuid.UUID | None = None,
    passport_id: uuid.UUID | None = None,
    tree_size: int = 5,
    root_hash: str = "a" * 64,
    signature: str = "b" * 64,
    signed_at: datetime | None = None,
) -> AuditTreeHead:
    """构造一个未持久化但 ``signed_at`` 已就绪的 STH（用于 anchor 测试）。

    锚定函数只读取字段值，不读 DB——所以无需把 STH flush 进 session。
    """
    return AuditTreeHead(
        user_id=user_id or uuid.uuid4(),
        passport_id=passport_id,
        tree_size=tree_size,
        root_hash=root_hash,
        signature=signature,
        signed_at=signed_at or datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC),
    )


# ===========================================================================
# 1. 空路径 / 路径未配置（保留正常返回，不当错误）
# ===========================================================================
class TestEmptyAnchorPath:
    def test_empty_string_path_returns_false_no_error(self) -> None:
        """``settings.AUDIT_STH_ANCHOR_PATH=""`` 是合法配置（"未启用锚定"）。

        让调用方可以无脑把 settings 字段直接传进来,无需自己分支判空。
        """
        sth = _make_sth()
        assert anchor_sth_to_file(sth, "") is False

    def test_empty_path_does_not_create_files(self, tmp_path: Path) -> None:
        sth = _make_sth()
        anchor_sth_to_file(sth, "")
        # tmp_path 下不应该出现任何残留文件
        assert list(tmp_path.iterdir()) == []


# ===========================================================================
# 2. 正常写入：JSONL 格式 + 全字段
# ===========================================================================
class TestSuccessfulAnchor:
    def test_writes_one_jsonl_line(self, tmp_path: Path) -> None:
        anchor = tmp_path / "sth.jsonl"
        sth = _make_sth()
        ok = anchor_sth_to_file(sth, str(anchor))
        assert ok is True
        assert anchor.exists()

        lines = [ln for ln in anchor.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 1
        # 每行必须是合法 JSON（unix 工具可消费）
        record = json.loads(lines[0])
        # 字段齐全
        assert set(record.keys()) == {
            "signed_at", "user_id", "passport_id",
            "tree_size", "root_hash", "signature",
        }

    def test_record_contains_correct_values(self, tmp_path: Path) -> None:
        anchor = tmp_path / "sth.jsonl"
        user_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        passport_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        sth = _make_sth(
            user_id=user_id,
            passport_id=passport_id,
            tree_size=42,
            root_hash="c" * 64,
            signature="d" * 64,
        )
        anchor_sth_to_file(sth, str(anchor))

        record = json.loads(anchor.read_text(encoding="utf-8").splitlines()[0])
        assert record["user_id"] == str(user_id)
        assert record["passport_id"] == str(passport_id)
        assert record["tree_size"] == 42
        assert record["root_hash"] == "c" * 64
        assert record["signature"] == "d" * 64
        # signed_at 是 ISO 8601 + 时区
        assert "+00:00" in record["signed_at"] or "Z" in record["signed_at"]

    def test_passport_id_none_serialized_as_json_null(self, tmp_path: Path) -> None:
        """用户级链 ``passport_id=None`` → 写入 JSON null（不是字符串 "None"）。"""
        anchor = tmp_path / "sth.jsonl"
        sth = _make_sth(passport_id=None)
        anchor_sth_to_file(sth, str(anchor))

        # 读出后必须是真 None
        record = json.loads(anchor.read_text(encoding="utf-8").splitlines()[0])
        assert record["passport_id"] is None


# ===========================================================================
# 3. 追加（不覆盖）+ 幂等（重复 STH 跳过）
# ===========================================================================
class TestAppendAndIdempotency:
    def test_multiple_distinct_sths_appended(self, tmp_path: Path) -> None:
        """三条不同 STH（tree_size 各异）→ 文件应有三行。"""
        anchor = tmp_path / "sth.jsonl"
        for size in (1, 2, 3):
            anchor_sth_to_file(_make_sth(tree_size=size), str(anchor))

        lines = [ln for ln in anchor.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 3
        sizes = [json.loads(ln)["tree_size"] for ln in lines]
        assert sizes == [1, 2, 3]

    def test_duplicate_sth_skipped(self, tmp_path: Path) -> None:
        """同 (user_id, passport_id, tree_size, root_hash) → 第二次返回 False，不写入。

        幂等让 scheduler 重启或测试反复调用时不堆积冗余记录。
        """
        anchor = tmp_path / "sth.jsonl"
        user_id = uuid.uuid4()
        sth1 = _make_sth(user_id=user_id, tree_size=5, root_hash="abc" * 21 + "a")
        sth2 = _make_sth(user_id=user_id, tree_size=5, root_hash="abc" * 21 + "a")

        ok1 = anchor_sth_to_file(sth1, str(anchor))
        ok2 = anchor_sth_to_file(sth2, str(anchor))

        assert ok1 is True
        assert ok2 is False  # 重复→跳过

        lines = [ln for ln in anchor.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 1

    def test_different_root_after_same_size_writes_new_line(
        self, tmp_path: Path
    ) -> None:
        """tree_size 相同但 root_hash 不同（理论上不该发生，做防御测试）→ 写入新行。

        合法的 tree_size 单调增；但若上游因某种原因签发了同 size 不同 root，
        我们把它当"链发生分叉/篡改"，必须留痕，不能视为重复。
        """
        anchor = tmp_path / "sth.jsonl"
        user_id = uuid.uuid4()
        sth1 = _make_sth(user_id=user_id, tree_size=5, root_hash="a" * 64)
        sth2 = _make_sth(user_id=user_id, tree_size=5, root_hash="b" * 64)

        anchor_sth_to_file(sth1, str(anchor))
        anchor_sth_to_file(sth2, str(anchor))

        lines = [ln for ln in anchor.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 2


# ===========================================================================
# 4. 父目录懒创建
# ===========================================================================
class TestParentDirectoryAutoCreate:
    def test_creates_missing_parent_directory(self, tmp_path: Path) -> None:
        """目标路径父目录不存在 → 自动 ``mkdir(parents=True)``。"""
        deep_path = tmp_path / "deep" / "nested" / "subdir" / "sth.jsonl"
        assert not deep_path.parent.exists()

        ok = anchor_sth_to_file(_make_sth(), str(deep_path))

        assert ok is True
        assert deep_path.exists()
        assert deep_path.parent.is_dir()


# ===========================================================================
# 5. I/O 失败：吞错 + False（不让 scheduler 崩）
# ===========================================================================
class TestIoFailureSwallowed:
    def test_unwritable_path_returns_false_not_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """模拟 ``Path.open`` 抛 ``PermissionError`` → 函数返回 False 不抛。

        生产中真实场景：只读挂载点 / 磁盘满 / 文件系统损坏。
        """
        anchor = tmp_path / "sth.jsonl"

        def _raise(*args, **kwargs):
            raise PermissionError("simulated permission denied")

        monkeypatch.setattr(Path, "open", _raise)

        ok = anchor_sth_to_file(_make_sth(), str(anchor))
        assert ok is False

    def test_corrupted_last_line_does_not_crash(self, tmp_path: Path) -> None:
        """文件最后一行不是合法 JSON → 当作"无最后记录"处理，继续追加新行。"""
        anchor = tmp_path / "sth.jsonl"
        # 写一个合法行 + 一个损坏行
        anchor.write_text(
            '{"signed_at": "2026-01-01T00:00:00+00:00", "user_id": "x", '
            '"passport_id": null, "tree_size": 1, "root_hash": "a", "signature": "b"}\n'
            "this-is-not-json\n",
            encoding="utf-8",
        )

        ok = anchor_sth_to_file(_make_sth(), str(anchor))

        # 即使最后一行损坏，新记录仍能追加
        assert ok is True
        lines = [ln for ln in anchor.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 3  # 原 2 + 新追加 1
        # 新追加的最后一行必须是合法 JSON
        json.loads(lines[-1])


# ===========================================================================
# 6. JSON 序列化健壮性
# ===========================================================================
class TestJsonSerializationRobustness:
    def test_uses_sort_keys_for_deterministic_output(self, tmp_path: Path) -> None:
        """``json.dumps(..., sort_keys=True)`` → 字段输出顺序固定。

        这让 ``diff`` / ``grep`` 等 unix 工具能稳定地对锚定文件做比对。
        """
        anchor = tmp_path / "sth.jsonl"
        anchor_sth_to_file(_make_sth(), str(anchor))

        line = anchor.read_text(encoding="utf-8").splitlines()[0]
        # 字段按字典序：passport_id < root_hash < signature < signed_at < tree_size < user_id
        # 实际只要是合法 JSON + sorted 即可，不强求精确顺序
        record = json.loads(line)
        keys = list(record.keys())
        assert keys == sorted(keys)
