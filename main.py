"""
AstrBot Essence Message Plugin - 神人语句加精插件
自动识别群聊中的神人语句或经典语句，并将其设为精华消息
支持自动分析和手动加精两种模式
"""

import json
import re
import time
import asyncio
from typing import Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.message_components import Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


class EssenceMessagePlugin(Star):
    """神人语句加精插件 - 自动识别神人语句并加精"""

    DEFAULT_JUDGE_PROMPT = """你是一个专业的群聊内容审核助手，负责从群聊消息中识别"神人语句"或"经典语句"。

## 任务说明
请分析以下群聊消息列表，从中识别出值得加精（设为精华消息）的内容。

## 加精标准（符合以下任一条件即可）：
1. **神人语句**: 含有深刻见解、独特观点、令人印象深刻的言论
2. **经典语句**: 幽默风趣、妙语连珠、引发共鸣的精彩发言
3. **高能时刻**: 精辟总结、犀利吐槽、让人会心一笑的句子
4. **知识分享**: 有价值的技术分享、经验总结、知识科普

## 排除标准（以下内容不适合加精）：
- 无意义的刷屏、重复内容
- 过于平淡的日常对话
- 广告、推广类内容
- 可能引起争议或不适的内容
- 纯表情符号或极短消息

## 消息列表（JSON格式）
{messages_json}

## 输出要求
请严格按照以下JSON格式输出，不要输出其他内容：
{
  "essence_ids": ["消息ID1", "消息ID2", ...],
  "reasons": {
    "消息ID1": "加精理由",
    "消息ID2": "加精理由"
  }
}

注意：
- essence_ids 列表最多包含 {max_essence} 个消息ID
- 如果没有合适的内容，返回空列表 {"essence_ids": [], "reasons": {}}
- 只返回你认为最值得加精的消息，宁缺毋滥
"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}

        # 基础配置
        basic = self.config.get("basic", {})
        self.auto_essence_enabled = basic.get("auto_essence_enabled", True)
        self.manual_essence_enabled = basic.get("manual_essence_enabled", True)
        self.message_threshold = basic.get("message_threshold", 50)
        self.max_essence_per_analysis = basic.get("max_essence_per_analysis", 3)
        self.group_whitelist = [str(g) for g in basic.get("group_whitelist", [])]

        # 模型配置
        model_config = self.config.get("model", {})
        self.judge_provider_id = model_config.get("judge_provider_id", "")

        # 提示词配置
        prompt_config = self.config.get("prompt", {})
        self.judge_prompt = prompt_config.get("judge_prompt", self.DEFAULT_JUDGE_PROMPT)

        # 指令配置
        commands = self.config.get("commands", {})
        self.manual_command = commands.get("manual_command", "加精")
        self.essence_by_id_command = commands.get("essence_by_id_command", "/加精")

        # 并发控制锁
        self._analysis_locks: dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        """插件初始化"""
        logger.info(
            f"神人语句加精插件初始化完成: "
            f"自动加精={self.auto_essence_enabled}, "
            f"手动加精={self.manual_essence_enabled}, "
            f"阈值={self.message_threshold}, "
            f"每次最多加精={self.max_essence_per_analysis}, "
            f"白名单={len(self.group_whitelist)}群"
        )

    def _get_group_lock(self, group_id: str) -> asyncio.Lock:
        """获取群的分析锁"""
        if group_id not in self._analysis_locks:
            self._analysis_locks[group_id] = asyncio.Lock()
        return self._analysis_locks[group_id]

    # ===== 自动加精模式 =====

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听群消息用于自动加精"""
        if not self.auto_essence_enabled:
            return

        # 跳过机器人自己的消息
        if event.get_sender_id() == event.get_self_id():
            return

        # 检查白名单
        group_id = str(event.get_group_id())
        if self.group_whitelist and group_id not in self.group_whitelist:
            return

        # 缓存消息
        await self._buffer_message(event)

        # 检查是否达到阈值
        buffer = await self._get_buffer(group_id)
        msg_count = len(buffer.get("messages", []))

        if msg_count >= self.message_threshold:
            lock = self._get_group_lock(group_id)
            if lock.locked():
                logger.debug(f"群 {group_id} 正在分析中，跳过本次触发")
                return
            async with lock:
                await self._analyze_and_essence(event, group_id, buffer)

    async def _buffer_message(self, event: AstrMessageEvent) -> None:
        """缓存群消息"""
        group_id = str(event.get_group_id())
        key = f"essence_buffer_{group_id}"

        buffer = await self.get_kv_data(key, {"messages": [], "last_analysis_time": 0})

        message_entry = {
            "message_id": event.message_obj.message_id,
            "sender_id": str(event.get_sender_id()),
            "sender_name": event.get_sender_name() or "匿名用户",
            "content": event.message_str,
            "timestamp": int(time.time()),
            "group_id": group_id,
        }

        buffer["messages"].append(message_entry)
        await self.put_kv_data(key, buffer)
        logger.debug(f"已缓存消息 {message_entry['message_id']} 来自群 {group_id}")

    async def _get_buffer(self, group_id: str) -> dict:
        """获取群消息缓冲区"""
        key = f"essence_buffer_{group_id}"
        return await self.get_kv_data(key, {"messages": [], "last_analysis_time": 0})

    async def _clear_buffer(self, group_id: str) -> None:
        """清空群消息缓冲区"""
        key = f"essence_buffer_{group_id}"
        await self.delete_kv_data(key)

    async def _analyze_and_essence(
        self, event: AstrMessageEvent, group_id: str, buffer: dict
    ) -> None:
        """调用 LLM 分析消息并加精"""
        messages_json = json.dumps(buffer["messages"], ensure_ascii=False, indent=2)

        prompt = self.judge_prompt.format(
            messages_json=messages_json,
            max_essence=self.max_essence_per_analysis,
        )

        logger.info(f"开始 LLM 分析群 {group_id} 的 {len(buffer['messages'])} 条消息")

        # 获取 LLM 提供商
        llm_resp = None
        try:
            if self.judge_provider_id:
                provider = self.context.get_provider_by_id(self.judge_provider_id)
                if provider:
                    llm_resp = await provider.text_chat(
                        prompt=prompt, contexts=[], image_urls=[]
                    )
                else:
                    logger.warning(
                        f"配置的模型提供商不存在: {self.judge_provider_id}, 使用默认模型"
                    )

            if not llm_resp:
                default_provider_id = await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
                if default_provider_id:
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=default_provider_id,
                        prompt=prompt,
                    )
                else:
                    logger.warning("无法获取当前聊天模型 ID")
                    await self._clear_buffer(group_id)
                    return

            if not llm_resp or not llm_resp.completion_text:
                logger.warning("LLM 返回空响应")
                await self._clear_buffer(group_id)
                return

        except Exception as e:
            logger.error(f"LLM 分析调用失败: {e}")
            await self._clear_buffer(group_id)
            return

        # 解析结果
        essence_ids, reasons = self._parse_llm_result(llm_resp.completion_text)

        if essence_ids:
            logger.info(f"LLM 识别出 {len(essence_ids)} 条神人语句")
            # 获取 bot 客户端并加精
            if isinstance(event, AiocqhttpMessageEvent):
                bot = event.bot
                for msg_id in essence_ids:
                    try:
                        await bot.call_action(
                            "set_essence_msg", message_id=int(msg_id)
                        )
                        reason = reasons.get(msg_id, "无理由")
                        logger.info(f"已加精消息 {msg_id}: {reason}")
                    except Exception as e:
                        logger.error(f"加精消息 {msg_id} 失败: {e}")
            else:
                logger.warning("非 aiocqhttp 平台事件，无法调用加精 API")

        # 清空缓冲区
        await self._clear_buffer(group_id)
        logger.info(f"群 {group_id} 分析完成，已清理缓冲区")

    def _parse_llm_result(
        self, response_text: str
    ) -> tuple[list[str], dict[str, str]]:
        """解析 LLM 返回结果"""
        try:
            # 去除 markdown 代码块
            cleaned = re.sub(
                r"^```(?:json)?\s*", "", response_text.strip(), flags=re.IGNORECASE
            )
            cleaned = re.sub(r"\s*```$", "", cleaned).strip()

            # 提取 JSON
            json_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group(0))
                essence_ids = result.get("essence_ids", [])
                reasons = result.get("reasons", {})
                return essence_ids, reasons

            return [], {}

        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失败: {e}")
            return [], {}

    # ===== 手动加精模式 =====

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def on_admin_message(self, event: AstrMessageEvent):
        """处理管理员的手动加精指令"""
        if not self.manual_essence_enabled:
            return

        # 处理回复消息加精
        await self._handle_reply_essence(event)

        # 处理指定消息ID加精
        await self._handle_id_essence(event)

    async def _handle_reply_essence(self, event: AstrMessageEvent) -> None:
        """处理回复消息加精（回复消息后发送指令）"""
        message_str = event.message_str.strip()
        if message_str != self.manual_command:
            return

        # 检查是否包含回复组件
        messages = event.get_messages()
        for comp in messages:
            if isinstance(comp, Reply):
                reply_id = str(comp.id)
                if reply_id:
                    await self._set_essence(event, reply_id)
                    return

        logger.debug("未找到被回复的消息")

    async def _handle_id_essence(self, event: AstrMessageEvent) -> None:
        """处理指定消息ID加精"""
        message_str = event.message_str.strip()

        if not message_str.startswith(self.essence_by_id_command):
            return

        # 解析消息ID
        parts = message_str.split()
        if len(parts) < 2:
            event.set_result(event.plain_result(f"用法: {self.essence_by_id_command} <消息ID>"))
            return

        msg_id = parts[1].strip()
        await self._set_essence(event, msg_id)

    async def _set_essence(self, event: AstrMessageEvent, message_id: str) -> None:
        """调用 OneBot API 设置精华消息"""
        if not isinstance(event, AiocqhttpMessageEvent):
            logger.warning("非 aiocqhttp 平台，无法调用加精 API")
            event.set_result(event.plain_result("当前平台不支持加精功能"))
            return

        bot = event.bot
        try:
            msg_id_int = int(message_id)
            await bot.call_action("set_essence_msg", message_id=msg_id_int)
            logger.info(f"手动加精成功: 消息ID={message_id}")
            event.set_result(event.plain_result(f"已将消息 {message_id} 设为精华"))
        except ValueError:
            logger.error(f"无效的消息ID: {message_id}")
            event.set_result(event.plain_result(f"无效的消息ID: {message_id}"))
        except Exception as e:
            logger.error(f"加精失败: {e}")
            event.set_result(event.plain_result(f"加精失败: {e}"))

    async def terminate(self) -> None:
        """插件卸载"""
        self._analysis_locks.clear()
        logger.info("神人语句加精插件已卸载")