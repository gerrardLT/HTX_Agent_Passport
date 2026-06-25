"""火山引擎 TTS 语音合成代理路由（volcengine-audio 官方 SDK + V3 WebSocket 双向接口）。

前端产品演示页面通过此端点调用火山引擎豆包语音合成 V3 API，
避免浏览器直接请求时遇到 CORS 限制和 API Key 泄露。

请求：POST /api/tts/synthesize  { text: str, voice_id?: str }
响应：{ audio_base64: str, encoding: str, duration_ms: float }

批量接口：POST /api/tts/batch  { segments: [{text}], voice_id?: str }
在同一连接中顺序合成多段，确保人声一致。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from typing import Optional

import websockets
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from volcengine_audio import (
    EventReceive,
    TTSBigmodelResourceType,
    VolcengineTTSFunctions,
)
from volcengine_audio.protocol import HOST

from app.core.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()

# ─── WebSocket 连接配置 ───

WS_URL = f"wss://{HOST}/api/v3/tts/bidirection"
RESOURCE_ID = TTSBigmodelResourceType.seed_tts_2_0.value  # "seed-tts-2.0"
WS_TIMEOUT = 30  # 整体超时（秒）
MODEL = "seed-tts-2.0-standard"  # 内置音色默认模型版本


# ─── 请求 / 响应模型 ───


class TTSSynthesizeRequest(BaseModel):
    """TTS 合成请求体。"""
    text: str = Field(..., min_length=1, max_length=2000, description="待合成的中文文本。")
    voice_id: Optional[str] = Field(
        default=None,
        description="声音 ID（speaker）；为空时使用服务端默认配置。",
    )
    speed_ratio: float = Field(default=1.0, ge=0.5, le=2.0, description="语速倍率。")
    volume_ratio: float = Field(default=1.0, ge=0.5, le=2.0, description="音量倍率。")
    pitch_ratio: float = Field(default=1.0, ge=0.5, le=2.0, description="音调倍率。")
    encoding: str = Field(default="mp3", description="音频编码格式：mp3 / wav / ogg_opus。")


class TTSSynthesizeResponse(BaseModel):
    """TTS 合成响应体。"""
    audio_base64: str = Field(..., description="Base64 编码的音频数据。")
    encoding: str = Field(..., description="音频编码格式。")
    duration_ms: float = Field(..., description="合成耗时（毫秒）。")


class TTSBatchSegment(BaseModel):
    """批量合成中的单段文本。"""
    text: str = Field(..., min_length=1, max_length=2000)


class TTSBatchRequest(BaseModel):
    """批量合成请求体 — 同一连接中顺序合成所有段落。"""
    segments: list[TTSBatchSegment] = Field(..., min_length=1, max_length=50)
    voice_id: Optional[str] = Field(default=None)
    speed_ratio: float = Field(default=1.0, ge=0.5, le=2.0)
    volume_ratio: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch_ratio: float = Field(default=1.0, ge=0.5, le=2.0)
    encoding: str = Field(default="mp3")


class TTSBatchSegmentResult(BaseModel):
    """批量合成中单段结果。"""
    audio_base64: str
    encoding: str


class TTSBatchResponse(BaseModel):
    """批量合成响应体。"""
    results: list[TTSBatchSegmentResult]
    duration_ms: float


# ─── 音频缓存 ───

_audio_cache: dict[str, tuple[str, str]] = {}
_CACHE_MAX_SIZE = 100


def _cache_key(text: str, voice_id: str) -> str:
    return f"{voice_id}:{text}"


# ─── 参数转换 ───


def _speed_to_rate(speed_ratio: float) -> int:
    """speed_ratio 1.0 → speech_rate 0; 2.0 → 100; 0.5 → -50。"""
    return max(-50, min(100, int((speed_ratio - 1.0) * 100)))


def _volume_to_rate(volume_ratio: float) -> int:
    return max(-50, min(100, int((volume_ratio - 1.0) * 100)))


def _pitch_to_rate(pitch_ratio: float) -> int:
    return max(-12, min(12, int((pitch_ratio - 1.0) * 12)))


# ─── WebSocket TTS 合成（使用 volcengine-audio 官方 SDK） ───


async def _ws_synthesize(
    api_key: str,
    speaker: str,
    text: str,
    encoding: str = "mp3",
    speech_rate: int = 0,
    loudness_rate: int = 0,
    pitch: int = 0,
) -> str:
    """通过 V3 WebSocket 双向接口合成语音，返回 base64 编码的音频。"""
    session_id = str(uuid.uuid4())

    additional_headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": RESOURCE_ID,
    }

    audio_params = {
        "format": encoding,
        "sample_rate": 24000,
    }
    if speech_rate:
        audio_params["speech_rate"] = speech_rate
    if loudness_rate:
        audio_params["loudness_rate"] = loudness_rate
    if pitch:
        audio_params["pitch"] = pitch

    audio_chunks: list[bytes] = []
    error_msg: str | None = None

    logger.info("TTS WS connecting: url=%s resource=%s speaker=%s", WS_URL, RESOURCE_ID, speaker)

    async with websockets.connect(
        WS_URL,
        additional_headers=additional_headers,
        open_timeout=10,
        close_timeout=5,
        compression=None,  # 禁用 permessage-deflate，避免协议版本不兼容
    ) as ws:
        # 1. StartConnection
        await ws.send(VolcengineTTSFunctions.start_connection_payload())

        # 2. 事件循环
        audio_recv_timeout = 2.0  # 收到首个音频后，等待更多音频的短超时
        got_audio = False

        while True:
            recv_timeout = audio_recv_timeout if got_audio else WS_TIMEOUT
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
            except asyncio.TimeoutError:
                if got_audio:
                    # 音频已接收完毕，服务端不再发送更多消息 → 正常结束
                    logger.info("TTS WS audio complete (no more messages after %.1fs)", audio_recv_timeout)
                    break
                error_msg = "WebSocket receive timeout (no audio)"
                break

            event, sid, payload = VolcengineTTSFunctions.extract_response_payload(msg)

            logger.info("TTS WS event=%s sid=%s payload_len=%d", event, sid, len(payload) if payload else 0)

            if event == EventReceive.ConnectionStarted:
                # 3. StartSession — 携带合成参数
                req_params = {
                    "text": text,
                    "speaker": speaker,
                    "model": MODEL,
                    "audio_params": audio_params,
                }
                await ws.send(
                    VolcengineTTSFunctions.start_session_payload(
                        session_id=session_id,
                        req_params=req_params,
                    )
                )

            elif event == EventReceive.SessionStarted:
                logger.info("TTS WS session started, sending TaskRequest...")
                # 4. TaskRequest — 发送合成文本
                await ws.send(
                    VolcengineTTSFunctions.task_request_payload(
                        session_id=session_id,
                        text=text,
                        speaker=speaker,
                        audio_params=audio_params,
                    )
                )

            elif event == EventReceive.TTSResponse:
                # 音频数据 — SDK 直接返回 raw bytes
                if payload:
                    audio_chunks.append(payload)
                    got_audio = True

            elif event == EventReceive.TTSSentenceStart:
                pass  # 句子开始

            elif event == EventReceive.TTSSentenceEnd:
                pass  # 句子结束

            elif event == EventReceive.SessionFinished:
                break  # 会话完成

            elif event == EventReceive.SessionFailed:
                error_msg = (
                    payload.get("message", "Session failed")
                    if isinstance(payload, dict)
                    else "Session failed"
                )
                break

            elif event == EventReceive.ConnectionFailed:
                error_msg = (
                    payload.get("message", "Connection failed")
                    if isinstance(payload, dict)
                    else "Connection failed"
                )
                break

        # 5. FinishSession + FinishConnection
        try:
            await ws.send(VolcengineTTSFunctions.finish_session_payload(session_id))
            await ws.send(VolcengineTTSFunctions.finish_connection_payload())
        except Exception:
            pass

    if error_msg:
        raise RuntimeError(error_msg)

    if not audio_chunks:
        raise RuntimeError("No audio data received from TTS API")

    combined = b"".join(audio_chunks)
    return base64.b64encode(combined).decode()


# ─── 端点 ───


@router.post("/synthesize", response_model=TTSSynthesizeResponse)
async def synthesize(req: TTSSynthesizeRequest) -> TTSSynthesizeResponse:
    """调用火山引擎 V3 WebSocket TTS API 合成语音并返回 Base64 音频。

    - 首次请求会真实调用 API，结果写入内存缓存
    - 后续相同 (text, voice_id) 的请求直接返回缓存
    """
    settings = get_settings()

    # 校验配置
    if not settings.VOLCENGINE_TTS_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="TTS service not configured. Set VOLCENGINE_TTS_API_KEY in .env",
        )

    voice_id = req.voice_id or settings.VOLCENGINE_TTS_VOICE_ID
    cache_key = _cache_key(req.text, voice_id)

    # 命中缓存
    if cache_key in _audio_cache:
        cached_audio, cached_enc = _audio_cache[cache_key]
        logger.debug("TTS cache hit: %s", cache_key[:40])
        return TTSSynthesizeResponse(
            audio_base64=cached_audio,
            encoding=cached_enc,
            duration_ms=0.0,
        )

    # 转换参数
    speech_rate = _speed_to_rate(req.speed_ratio)
    loudness_rate = _volume_to_rate(req.volume_ratio)
    pitch = _pitch_to_rate(req.pitch_ratio)

    t0 = time.monotonic()
    try:
        audio_b64 = await _ws_synthesize(
            api_key=settings.VOLCENGINE_TTS_API_KEY,
            speaker=voice_id,
            text=req.text,
            encoding=req.encoding,
            speech_rate=speech_rate,
            loudness_rate=loudness_rate,
            pitch=pitch,
        )
    except Exception as exc:
        logger.error("Volcengine TTS WebSocket error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"TTS synthesis failed: {exc}",
        ) from exc

    elapsed_ms = (time.monotonic() - t0) * 1000

    # 写入缓存
    if len(_audio_cache) >= _CACHE_MAX_SIZE:
        keys = list(_audio_cache.keys())
        for k in keys[: _CACHE_MAX_SIZE // 2]:
            del _audio_cache[k]
    _audio_cache[cache_key] = (audio_b64, req.encoding)

    logger.info(
        "TTS synthesized: %d chars -> %d bytes audio in %.0fms",
        len(req.text), len(audio_b64), elapsed_ms,
    )

    return TTSSynthesizeResponse(
        audio_base64=audio_b64,
        encoding=req.encoding,
        duration_ms=round(elapsed_ms, 1),
    )


# ─── 批量合成（同一连接顺序合成，确保人声一致） ───


async def _ws_batch_synthesize(
    api_key: str,
    speaker: str,
    texts: list[str],
    encoding: str = "mp3",
    speech_rate: int = 0,
    loudness_rate: int = 0,
    pitch: int = 0,
) -> list[str]:
    """顺序合成多段文本，确保人声统一。

    火山引擎 V3 API 不支持同一连接多 session，因此每段独立连接但串行处理。
    统一人声靠相同的 speaker + model 参数保证。
    """
    results: list[str] = []
    logger.info("TTS batch: %d segments, speaker=%s (sequential)", len(texts), speaker)

    for i, text in enumerate(texts):
        try:
            audio_b64 = await _ws_synthesize(
                api_key=api_key,
                speaker=speaker,
                text=text,
                encoding=encoding,
                speech_rate=speech_rate,
                loudness_rate=loudness_rate,
                pitch=pitch,
            )
            results.append(audio_b64)
            logger.info("TTS batch segment %d/%d done", i + 1, len(texts))
        except Exception as exc:
            raise RuntimeError(f"Segment {i} failed: {exc}") from exc

    return results


@router.post("/batch", response_model=TTSBatchResponse)
async def batch_synthesize(req: TTSBatchRequest) -> TTSBatchResponse:
    """批量合成：在同一个 WebSocket 连接中顺序合成所有段落。

    确保人声统一（同一连接 = 同一模型状态 = 一致的音色特征）。
    """
    settings = get_settings()

    if not settings.VOLCENGINE_TTS_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="TTS service not configured. Set VOLCENGINE_TTS_API_KEY in .env",
        )

    voice_id = req.voice_id or settings.VOLCENGINE_TTS_VOICE_ID
    texts = [seg.text for seg in req.segments]

    speech_rate = _speed_to_rate(req.speed_ratio)
    loudness_rate = _volume_to_rate(req.volume_ratio)
    pitch = _pitch_to_rate(req.pitch_ratio)

    t0 = time.monotonic()
    try:
        audio_list = await _ws_batch_synthesize(
            api_key=settings.VOLCENGINE_TTS_API_KEY,
            speaker=voice_id,
            texts=texts,
            encoding=req.encoding,
            speech_rate=speech_rate,
            loudness_rate=loudness_rate,
            pitch=pitch,
        )
    except Exception as exc:
        logger.error("Volcengine TTS batch error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"TTS batch synthesis failed: {exc}",
        ) from exc

    elapsed_ms = (time.monotonic() - t0) * 1000

    results = [
        TTSBatchSegmentResult(audio_base64=audio, encoding=req.encoding)
        for audio in audio_list
    ]

    logger.info(
        "TTS batch complete: %d segments in %.0fms",
        len(results), elapsed_ms,
    )

    return TTSBatchResponse(results=results, duration_ms=round(elapsed_ms, 1))
