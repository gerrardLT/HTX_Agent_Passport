"""生成 Ed25519 私钥/公钥对，供 STH 签名使用（Phase 2 生产升级）。

用法
----
::

    python -m scripts.generate_ed25519_key --out ./keys/sth

生成的文件：

- ``./keys/sth_priv.pem``：PEM PKCS8 私钥（**保密 + 仅文件所有者可读**）
- ``./keys/sth_pub.pem``：PEM 公钥（**可公开发布**）
- ``./keys/sth_pub.hex``：32 字节公钥的 hex 表示（一行，方便贴 README / git）

然后配置 ``backend/.env``::

    AUDIT_STH_SIGNING_ALGO=ed25519
    AUDIT_STH_ED25519_PRIVATE_KEY_PATH=/abs/path/to/keys/sth_priv.pem
    AUDIT_STH_ED25519_PUBLIC_KEY_PATH=/abs/path/to/keys/sth_pub.pem

重启 backend 后，新签发的 STH 签名都会带 ``ed25519:`` 前缀；
旧的 ``hmac:`` / 无前缀签名仍可正常验证（向后兼容）。
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def generate_keypair(out_dir: Path, prefix: str = "sth") -> tuple[Path, Path, Path]:
    """生成密钥对并写入指定目录；返回 (priv_path, pub_path, hex_path)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    priv_path = out_dir / f"{prefix}_priv.pem"
    pub_path = out_dir / f"{prefix}_pub.pem"
    hex_path = out_dir / f"{prefix}_pub.hex"

    if priv_path.exists():
        raise FileExistsError(
            f"refusing to overwrite existing private key: {priv_path}\n"
            "delete it manually if you really mean to rotate."
        )

    private_key = Ed25519PrivateKey.generate()

    priv_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    # 私钥仅文件所有者可读（POSIX；Windows 上 chmod 是 best-effort）
    try:
        priv_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except (OSError, NotImplementedError):
        pass

    public_key = private_key.public_key()
    pub_path.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    pub_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    hex_path.write_text(pub_raw.hex() + "\n", encoding="utf-8")

    return priv_path, pub_path, hex_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Ed25519 keypair for STH signing")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("./keys"),
        help="Output directory (default: ./keys)",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default="sth",
        help="File prefix (default: sth)",
    )
    args = parser.parse_args()

    try:
        priv_path, pub_path, hex_path = generate_keypair(args.out, args.prefix)
    except FileExistsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Ed25519 keypair generated:")
    print(f"  private key: {priv_path.resolve()}")
    print(f"  public key:  {pub_path.resolve()}")
    print(f"  public hex:  {hex_path.resolve()}")
    print()
    print("Next steps:")
    print(f"  1. Set in backend/.env:")
    print(f"     AUDIT_STH_SIGNING_ALGO=ed25519")
    print(f"     AUDIT_STH_ED25519_PRIVATE_KEY_PATH={priv_path.resolve()}")
    print(f"     AUDIT_STH_ED25519_PUBLIC_KEY_PATH={pub_path.resolve()}")
    print(f"  2. Restart backend.")
    print(f"  3. Publish {hex_path.name} to your README / public git for")
    print(f"     external auditors to verify STH signatures independently.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
