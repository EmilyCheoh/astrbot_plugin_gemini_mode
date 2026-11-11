"""Runtime switch for Gemini's native API transport, shared by Den and QQ."""

from copy import copy
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.provider.register import provider_cls_map
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


class GeminiMode(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # First upgrade from mode.txt: migrate the old selection once, then retire the file.
        if getattr(config, "first_deploy", False):
            old_file = (
                Path(get_astrbot_data_path())
                / "plugin_data" / "astrbot_plugin_gemini_mode" / "mode.txt"
            )
            if old_file.is_file():
                try:
                    old_mode = old_file.read_text(encoding="utf-8").strip()
                    if old_mode in ("stream", "legacy"):
                        self.config["mode"] = old_mode
                        self.config.save_config()
                    old_file.unlink()
                except OSError as exc:
                    logger.warning("[gemini-mode] 迁移旧模式记录失败：%s", exc)
        self._apply()

    @staticmethod
    def _gemini_class():
        info = provider_cls_map.get("googlegenai_chat_completion")
        cls = getattr(info, "cls_type", None)
        if cls is not None and hasattr(cls, "USE_STREAMED_NONSTREAM_REQUESTS"):
            return cls
        return None

    @staticmethod
    def _install_stream_bridge(cls):
        """Let legacy mode use Gemini's non-streaming API even in AstrBot's streaming runner."""
        if hasattr(cls, "_gemini_mode_original_query_stream"):
            return

        cls._gemini_mode_original_query_stream = cls._query_stream

        async def _query_stream_with_mode(provider, payloads, tools):
            if provider.USE_STREAMED_NONSTREAM_REQUESTS:
                async for response in provider._gemini_mode_original_query_stream(payloads, tools):
                    yield response
                return

            # AstrBot may call text_chat_stream() even when this plugin requests legacy.
            # Make exactly one real non-streaming Gemini request, then adapt its full
            # answer to the runner's chunk + final-response protocol.
            response = await provider._query_legacy(payloads, tools)
            if response.role == "assistant" and (
                response.reasoning_content or response.result_chain or response.completion_text
            ):
                chunk = copy(response)
                chunk.is_chunk = True
                yield chunk
            yield response

        cls._query_stream = _query_stream_with_mode

    def _apply(self) -> bool:
        cls = self._gemini_class()
        if cls is None:
            logger.warning("[gemini-mode] Gemini 原生适配器或运行时开关不存在")
            return False
        self._install_stream_bridge(cls)
        mode = self.config.get("mode", "stream")
        cls.USE_STREAMED_NONSTREAM_REQUESTS = mode == "stream"
        logger.info("[gemini-mode] Gemini 底层模式：%s", mode)
        return True

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        # Provider 在冷启动过程中可能晚于插件注册。
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
            "check": "status",
            "mode": "status",
        }.get(mode, mode)

        cls = self._gemini_class()
        if cls is None:
            yield event.plain_result("⚠️ 当前 Gemini 代码缺少运行时切换开关，模式未改变。")
            return

        if mode in ("", "status"):
            actual = "流式（stream）" if cls.USE_STREAMED_NONSTREAM_REQUESTS else "非流（legacy）"
            yield event.plain_result(f"🐈‍⬛ Gemini 底层模式：{actual}（小窝与 QQ 共用）")
            return

        if mode not in ("stream", "legacy"):
            yield event.plain_result(
                "用法：/gemini 流式（流） | /gemini 非流 | /gemini status（状态/check）"
            )
            return

        # 先保存原生插件配置，再更新正在运行的 Provider 类开关。
        previous = self.config.get("mode", "stream")
        self.config["mode"] = mode
        try:
            self.config.save_config()
        except OSError as exc:
            self.config["mode"] = previous
            logger.error("[gemini-mode] 保存模式失败：%s", exc)
            yield event.plain_result("⚠️ 保存模式失败，本次没有切换。")
            return

        cls.USE_STREAMED_NONSTREAM_REQUESTS = mode == "stream"
        display = "流式" if mode == "stream" else "非流"
        yield event.plain_result(
            f"✅ Gemini 已切换到{display}，下一次请求生效；无需重启 AstrBot。"
        )

    async def terminate(self):
        # A disabled/reloaded plugin should not leave its streaming bridge installed.
        cls = self._gemini_class()
        if cls is not None and hasattr(cls, "_gemini_mode_original_query_stream"):
            cls._query_stream = cls._gemini_mode_original_query_stream
            del cls._gemini_mode_original_query_stream
            cls.USE_STREAMED_NONSTREAM_REQUESTS = True
