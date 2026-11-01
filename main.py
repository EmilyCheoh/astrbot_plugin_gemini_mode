"""Switch the Gemini native transport at runtime, without touching AstrBot's streaming UI."""
from asyncio import base_events
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.provider.register import provider_cls_map
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


class GeminiMode(Star):
    """Use /gmode stream, /gmode legacy or /gmode status in Den or QQ."""

    def __init__(self, context: Context):
        super().__init__(context)
        self._mode_file = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "astrbot_plugin_gemini_mode"
            / "mode.txt"
        )
        self._mode = "stream"
        if self._mode_file.is_file():
            stored = self._mode_file.read_text(encoding="utf-8").strip()
            if stored in ("stream", "legacy"):
                self._mode = stored
        self._apply()

    @staticmethod
    def _gemini_class():
        info = provider_cls_map.get("googlegenai_chat_completion")
        cls = getattr(info, "cls_type", None)
        if cls is not None and hasattr(cls, "USE_STREAMED_NONSTREAM_REQUESTS"):
            return cls
        return None

    def _apply(self) -> bool:
        cls = self._gemini_class()
        if cls is None:
            logger.warning("[gmode] Gemini 原生适配器或可切换开关不存在")
            return False
        cls.USE_STREAMED_NONSTREAM_REQUESTS = self._mode == "stream"
        logger.info("[gmode] Gemini 底层模式: %s", self._mode)
        return True

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        # Also apply after all providers have been registered on a cold start.
        self._apply()

    @filter.command("gemini")
    async def gmode(self, event: AstrMessageEvent, mode: str = ""):
        """切换 Gemini 原生底层请求模式；不改变小窝/QQ 的显示方式。"""
        mode = mode.strip().lower()
        mode = {
            "流式": "stream",
            "流": "stream",
            "非流": "legacy",
            "状态": "status",
        }.get(mode, mode)
        cls = self._gemini_class()
        if cls is None:
            yield event.plain_result("⚠️ 当前 Gemini 代码缺少运行时切换开关，模式未改变。")
            return

        if mode in ("", "status"):
            actual = "stream" if cls.USE_STREAMED_NONSTREAM_REQUESTS else "legacy"
            yield event.plain_result(f"🐈‍⬛ Gemini 底层模式：{actual}（小窝与 QQ 共用）")
            return

        if mode not in ("stream", "legacy"):
            yield event.plain_result("用法：/gemini 流式（流） | /gemini 非流 | /gemini status（状态）")
            return

        # Switch the class attribute used by the *existing* provider instances.
        cls.USE_STREAMED_NONSTREAM_REQUESTS = mode == "stream"
        self._mode = mode
        try:
            self._mode_file.parent.mkdir(parents=True, exist_ok=True)
            self._mode_file.write_text(mode + "\n", encoding="utf-8")
        except OSError as exc:
            logger.error("[gmode] 切换已生效，但保存失败：%s", exc)
            yield event.plain_result(f"✅ 已切换到 {mode}，但重启后可能恢复默认模式。")
            return

        yield event.plain_result(
            f"✅ Gemini 已切换到 {mode}，下一次请求生效；无需重启 AstrBot。"
        )
