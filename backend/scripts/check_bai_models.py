"""校验 B.AI 连通性与可用模型列表。

用途
----
填入真实 BAI_API_KEY 后运行本脚本，确认：
1. API Key 有效（能通过认证）。
2. B.AI 实际返回的模型 ID 列表——确认 deepseek-v4-flash 的准确 ID。
3. 用配置的 BAI_MODEL 做一次最小 chat 调用，验证端到端可用。

运行
----
    cd backend
    python scripts/check_bai_models.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让脚本能 import app 包
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_BACKEND_DIR / ".env")

import httpx  # noqa: E402

from app.core.config import get_settings  # noqa: E402


def main() -> int:
    settings = get_settings()
    api_url = settings.BAI_API_URL.rstrip("/")
    api_key = settings.BAI_API_KEY
    model = settings.BAI_MODEL

    if not api_key:
        print("[ERROR] BAI_API_KEY 为空。请在 backend/.env 填入 sk-xxx 后重试。")
        return 1

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ---- 1. 列出可用模型 ----
    print(f"[1/2] GET {api_url}/models")
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{api_url}/models", headers=headers)
        if resp.status_code != 200:
            print(f"[ERROR] 列模型失败 status={resp.status_code}: {resp.text[:300]}")
            return 1
        data = resp.json()
        models = [m.get("id") for m in data.get("data", [])]
        print(f"  可用模型（{len(models)} 个）:")
        for m in models:
            marker = "  <-- 当前配置" if m == model else ""
            print(f"    - {m}{marker}")
        if model not in models:
            print(
                f"\n[WARN] 当前 BAI_MODEL={model!r} 不在返回列表里。"
                "请从上面挑一个准确的 deepseek 模型 ID 填入 .env。"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] 列模型请求异常: {exc!r}")
        return 1

    # ---- 2. 最小 chat 调用 ----
    print(f"\n[2/2] POST {api_url}/chat/completions (model={model})")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You reply with a single word."},
            {"role": "user", "content": "Say OK."},
        ],
        "temperature": 0,
        "max_tokens": 16,
    }
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{api_url}/chat/completions", json=body, headers=headers
            )
        if resp.status_code != 200:
            print(f"[ERROR] chat 调用失败 status={resp.status_code}: {resp.text[:300]}")
            return 1
        content = resp.json()["choices"][0]["message"]["content"]
        print(f"  模型响应: {content!r}")
        print("\n[OK] B.AI 连通性验证通过，模型可用。")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] chat 请求异常: {exc!r}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
