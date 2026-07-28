"""Edge-TTS 语音合成模块。

将 agent 回复的文本转为语音，保存为 MP3 文件。
"""

import asyncio
import logging
import os
from pathlib import Path

import edge_tts

from config import settings

logger = logging.getLogger(__name__)

# 输出目录
OUTPUT_DIR = Path("output_audio")
OUTPUT_DIR.mkdir(exist_ok=True)


async def _async_speak(text: str, voice: str, filename: str) -> str:
    """异步执行 TTS 并返回文件路径。"""
    path = str(OUTPUT_DIR / filename)
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(path)
    return path


def speak(text: str, filename: str | None = None) -> str | None:
    """将文本合成为语音并保存。

    Args:
        text: 要朗读的文本。
        filename: 输出文件名，默认自动生成。

    Returns:
        str: 音频文件路径；若 TTS 未启用或文本为空则返回 None。
    """
    if not settings.tts_enabled:
        return None
    if not text or not text.strip():
        return None

    voice = settings.tts_voice
    if not filename:
        # 用文本前 30 个字符的哈希作为文件名
        safe = str(hash(text[:30]))
        filename = f"agent_{safe[:12]}.mp3"

    try:
        path = asyncio.run(_async_speak(text, voice, filename))
        logger.info("TTS saved to %s", path)
        return path
    except Exception:
        logger.exception("TTS failed")
        return None
