from utils.plugin_base import PluginBase
from utils.decorators import on_text_message, on_file_message, on_article_message
import aiohttp
import asyncio
import re
import os
import tomllib
import time
from loguru import logger
from typing import Dict, Optional, TYPE_CHECKING
import json
import html
import xml.etree.ElementTree as ET

# 类型提示导入
if TYPE_CHECKING:
    from WechatAPI import WechatAPIClient

class AutoSummary(PluginBase):
    description = "自动总结文本内容和卡片消息"
    author = "老夏的金库"
    version = "1.0.1"

    URL_PATTERN = r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[-\w./?=&]*'
    # 总结命令的触发词
    SUMMARY_TRIGGERS = ["总结", "总结链接", "总结内容", "总结一下", "帮我总结", "summarize"]

    def __init__(self):
        super().__init__()
        self.name = "AutoSummary"

        config_path = os.path.join(os.path.dirname(__file__), "config.toml")
        with open(config_path, "rb") as f:
            config = tomllib.load(f)

        self.config = config.get("AutoSummary", {})
        dify_config = self.config.get("Dify", {})
        self.dify_enable = dify_config.get("enable", False)
        self.dify_api_key = dify_config.get("api-key", "")
        self.dify_base_url = dify_config.get("base-url", "")
        self.http_proxy = dify_config.get("http-proxy", "")

        settings = self.config.get("Settings", {})
        self.max_text_length = settings.get("max_text_length", 8000)
        self.black_list = settings.get("black_list", [])
        self.white_list = settings.get("white_list", [])

        # 存储最近的链接和卡片信息
        self.recent_urls = {}  # 格式: {chat_id: {"url": url, "timestamp": timestamp}}
        self.recent_cards = {}  # 格式: {chat_id: {"info": card_info, "timestamp": timestamp}}
        # 链接和卡片的过期时间（秒）
        self.expiration_time = 300  # 5分钟

        self.http_session = aiohttp.ClientSession()

        if not self.dify_enable or not self.dify_api_key or not self.dify_base_url:
            logger.warning("Dify配置不完整，自动总结功能将被禁用")
            self.dify_enable = False

    async def close(self):
        if self.http_session:
            await self.http_session.close()
            logger.info("HTTP会话已关闭")

    def _check_url(self, url: str) -> bool:
        stripped_url = url.strip()
        if not stripped_url.startswith(('http://', 'https://')):
            return False
        if self.white_list and not any(stripped_url.startswith(white_url) for white_url in self.white_list):
            return False
        if any(stripped_url.startswith(black_url) for black_url in self.black_list):
            return False
        return True

    # 检查是否是总结命令
    def _is_summary_command(self, content: str) -> bool:
        content = content.strip().lower()
        return any(trigger in content for trigger in self.SUMMARY_TRIGGERS)

    # 清理过期的链接和卡片
    def _clean_expired_items(self):
        current_time = time.time()
        # 清理过期的URL
        for chat_id in list(self.recent_urls.keys()):
            if current_time - self.recent_urls[chat_id]["timestamp"] > self.expiration_time:
                del self.recent_urls[chat_id]

        # 清理过期的卡片
        for chat_id in list(self.recent_cards.keys()):
            if current_time - self.recent_cards[chat_id]["timestamp"] > self.expiration_time:
                del self.recent_cards[chat_id]

    async def _fetch_url_content(self, url: str) -> Optional[str]:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            }
            # 设置超时参数
            timeout = aiohttp.ClientTimeout(total=30)  # 30秒总超时

            # 先检查是否有重定向，获取最终URL
            final_url = url
            try:
                # 只发送HEAD请求来检查重定向，不获取实际内容
                async with self.http_session.head(url, headers=headers, allow_redirects=True, timeout=timeout) as head_response:
                    if head_response.status == 200:
                        final_url = str(head_response.url)
                        if final_url != url:
                            logger.info(f"检测到重定向: {url} -> {final_url}")
            except Exception as e:
                logger.warning(f"检查重定向失败: {e}, 使用原始URL")

            # 使用 Jina AI 获取内容（使用最终URL）
            logger.info(f"使用 Jina AI 获取内容: {final_url}")
            try:
                jina_url = f"https://r.jina.ai/{final_url}"
                async with self.http_session.get(jina_url, headers=headers, timeout=timeout) as jina_response:
                    if jina_response.status == 200:
                        content = await jina_response.text()
                        logger.info(f"从 Jina AI 获取内容成功: {jina_url}, 内容长度: {len(content)}")
                        return content
                    else:
                        logger.error(f"从 Jina AI 获取内容失败: {jina_response.status}, URL: {jina_url}")
            except Exception as e:
                logger.error(f"使用Jina AI获取内容失败: {e}")

            # 如果 Jina AI 失败，尝试直接获取
            logger.info(f"Jina AI 失败，尝试直接获取: {final_url}")
            try:
                async with self.http_session.get(final_url, headers=headers, timeout=timeout) as response:
                    if response.status != 200:
                        logger.error(f"直接获取URL失败: {response.status}, URL: {final_url}")
                        return None

                    content = await response.text()
                    if content and len(content) > 500:  # 确保内容有足够长度
                        logger.info(f"直接从URL获取内容成功: {final_url}, 内容长度: {len(content)}")
                        return content
            except Exception as e:
                logger.warning(f"直接获取内容失败: {e}")

            # 尝试使用备用方法直接获取
            return await self._fetch_url_content_direct(final_url)
        except asyncio.TimeoutError:
            logger.error(f"获取URL内容超时: URL: {url}")
            return None
        except Exception as e:
            logger.error(f"获取URL内容时出错: {e}, URL: {url}")
            return None

    async def _fetch_url_content_direct(self, url: str) -> Optional[str]:
        """直接获取URL内容的备用方法"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache"
            }
            timeout = aiohttp.ClientTimeout(total=30)  # 30秒总超时

            logger.info(f"备用方法尝试获取: {url}")
            async with self.http_session.get(url, headers=headers, timeout=timeout, allow_redirects=True) as response:
                if response.status != 200:
                    logger.warning(f"备用方法获取失败: {response.status}, URL: {url}")
                    return None

                content_type = response.headers.get('Content-Type', '')
                logger.info(f"内容类型: {content_type}")

                # 尝试获取文本内容，即使不是标准的HTML或JSON
                try:
                    content = await response.text()
                    if content and len(content) > 500:  # 确保内容有足够长度
                        logger.info(f"备用方法获取内容成功: {url}, 内容长度: {len(content)}")
                        return content
                except Exception as text_error:
                    logger.warning(f"获取文本内容失败: {text_error}")

                return None
        except Exception as e:
            logger.error(f"备用方法获取URL内容失败: {e}")
            return None

    async def _send_to_dify(self, content: str, is_xiaohongshu: bool = False) -> Optional[str]:
        if not self.dify_enable:
            return None
        try:
            content = content[:self.max_text_length]
            if is_xiaohongshu:
                prompt = f"""请对以下小红书笔记进行总结，关注以下方面：
1. 📝 一句话概括笔记主要内容
2. 🔑 核心要点（3-5点）
3. 💡 作者的主要观点或建议
4. 🏷️ 相关标签（2-3个）

原文内容：
{content}
"""
            else:
                prompt = f"""请对以下内容进行总结：
1. 📝 一句话总结
2. 🔑 关键要点（3-5点）
3. 🏷️ 相关标签（2-3个）

原文内容：
{content}
"""
            headers = {
                "Authorization": f"Bearer {self.dify_api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "inputs": {},
                "query": prompt,
                "response_mode": "blocking",
                "conversation_id": None,
                "user": "auto_summary"
            }
            url = f"{self.dify_base_url}/chat-messages"
            async with self.http_session.post(
                url=url,
                headers=headers,
                json=payload,
                proxy=self.http_proxy if self.http_proxy else None
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get("answer", "")
                else:
                    error_text = await response.text()
                    logger.error(f"调用Dify API失败: {response.status} - {error_text}")
                    return None
        except Exception as e:
            logger.error(f"调用Dify API时出错: {e}")
            return None

    def _process_xml_message(self, message: Dict) -> Optional[Dict]:
        try:
            content = message.get("Content", "")
            msg_id = message.get('MsgId', '')
            logger.info(f"插件处理XML消息: MsgId={msg_id}")

            # 检查消息类型
            msg_type = message.get("MsgType", 0)
            logger.info(f"消息类型: {msg_type}")

            # 检查内容是否为XML
            if not content.strip().startswith('<'):
                logger.warning("消息内容不是XML格式")
                return None

            logger.debug(f"完整XML内容: {content}")

            try:
                root = ET.fromstring(content)
                logger.info(f"解析XML根节点: {root.tag}")

                # 记录所有子节点以便调试
                for child in root:
                    logger.debug(f"子节点: {child.tag}")
            except ET.ParseError as e:
                logger.error(f"XML解析错误: {str(e)}")
                logger.error(f"XML内容片段: {content[:200]}...")
                return None

            appmsg = root.find('appmsg')
            if appmsg is None:
                logger.warning("未找到 appmsg 节点")
                return None

            logger.info("找到 appmsg 节点")

            # 记录appmsg的所有子节点
            for child in appmsg:
                logger.debug(f"appmsg子节点: {child.tag} = {child.text if child.text else ''}")

            title_elem = appmsg.find('title')
            des_elem = appmsg.find('des')
            url_elem = appmsg.find('url')
            type_elem = appmsg.find('type')

            title = title_elem.text if title_elem is not None and title_elem.text else ""
            description = des_elem.text if des_elem is not None and des_elem.text else ""
            url = url_elem.text if url_elem is not None and url_elem.text else None
            type_value = type_elem.text if type_elem is not None and type_elem.text else ""

            logger.info(f"提取的标题: {title}")
            logger.info(f"提取的描述: {description}")
            logger.info(f"提取的URL: {url}")
            logger.info(f"消息类型值: {type_value}")

            if url is None or not url.strip():
                logger.warning("URL为空，跳过处理")
                return None

            url = html.unescape(url)
            logger.info(f"处理后的URL: {url}")

            # 检查是否是小红书
            is_xiaohongshu = '<appname>小红书</appname>' in content
            if is_xiaohongshu:
                logger.info("检测到小红书卡片")

            result = {
                'title': title,
                'description': description,
                'url': url,
                'is_xiaohongshu': is_xiaohongshu,
                'type': type_value
            }
            logger.info(f"提取的信息: {result}")
            return result

        except ET.ParseError as e:
            logger.error(f"XML解析错误: {str(e)}")
            logger.error(f"XML内容片段: {content[:200] if 'content' in locals() else ''}...")
            return None
        except Exception as e:
            logger.error(f"处理XML消息时出错: {str(e)}")
            logger.exception(e)
            return None

    async def _process_url(self, url: str) -> Optional[str]:
        try:
            url_content = await self._fetch_url_content(url)
            if not url_content:
                return None
            return await self._send_to_dify(url_content)
        except Exception as e:
            logger.error(f"处理URL时出错: {e}")
            return None

    async def _handle_card_message(self, bot: 'WechatAPIClient', chat_id: str, info: Dict) -> bool:
        try:
            # 发送正在处理的消息
            await bot.send_text_message(chat_id, "🔍 正在获取卡片内容，请稍候...")

            # 获取URL内容
            url = info['url']
            logger.info(f"开始获取卡片URL内容: {url}")
            url_content = await self._fetch_url_content(url)

            if not url_content:
                logger.warning(f"无法获取卡片内容: {url}")
                await bot.send_text_message(chat_id, "❌ 抱歉，无法获取卡片内容")
                return False

            logger.info(f"成功获取卡片内容，长度: {len(url_content)}")

            # 构建要总结的内容
            content_to_summarize = f"""
标题：{info['title']}
描述：{info['description']}
正文：{url_content}
"""

            # 发送正在生成总结的消息
            await bot.send_text_message(chat_id, "🔍 正在为您生成内容总结，请稍候...")

            # 调用Dify API生成总结
            is_xiaohongshu = info.get('is_xiaohongshu', False)
            logger.info(f"开始生成总结, 是否小红书: {is_xiaohongshu}")
            summary = await self._send_to_dify(content_to_summarize, is_xiaohongshu=is_xiaohongshu)

            if not summary:
                logger.error("生成总结失败")
                await bot.send_text_message(chat_id, "❌ 抱歉，生成总结失败")
                return False

            logger.info(f"成功生成总结，长度: {len(summary)}")

            # 根据卡片类型设置前缀
            prefix = "🎯 小红书笔记总结如下" if is_xiaohongshu else "🎯 卡片内容总结如下"

            # 发送总结
            await bot.send_text_message(chat_id, f"{prefix}：\n\n{summary}")
            logger.info("总结已发送")
            return False  # 阻止后续处理

        except Exception as e:
            logger.error(f"处理卡片消息时出错: {e}")
            logger.exception(e)  # 记录完整堆栈信息
            await bot.send_text_message(chat_id, "❌ 抱歉，处理卡片内容时出现错误")
            return False

    @on_text_message(priority=50)
    async def handle_text_message(self, bot: 'WechatAPIClient', message: Dict) -> bool:
        if not self.dify_enable:
            return True

        content = message.get("Content", "")
        chat_id = message.get("FromWxid", "")

        logger.info(f"收到文本消息: chat_id={chat_id}, content={content[:100]}...")
        content = html.unescape(content)

        # 清理过期的链接和卡片
        self._clean_expired_items()

        # 检查是否是总结命令
        if self._is_summary_command(content):
            logger.info(f"检测到总结命令: {content}")

            # 检查是否有最近的URL
            if chat_id in self.recent_urls:
                url = self.recent_urls[chat_id]["url"]
                logger.info(f"开始总结最近的URL: {url}")
                try:
                    await bot.send_text_message(chat_id, "🔍 正在为您生成内容总结，请稍候...")
                    summary = await self._process_url(url)
                    if summary:
                        await bot.send_text_message(chat_id, f"🎯 内容总结如下：\n\n{summary}")
                        # 总结后删除该URL
                        del self.recent_urls[chat_id]
                        return False
                    else:
                        await bot.send_text_message(chat_id, "❌ 抱歉，生成总结失败")
                        return False
                except Exception as e:
                    logger.error(f"处理URL时出错: {e}")
                    await bot.send_text_message(chat_id, "❌ 抱歉，处理过程中出现错误")
                    return False

            # 检查是否有最近的卡片
            elif chat_id in self.recent_cards:
                card_info = self.recent_cards[chat_id]["info"]
                logger.info(f"开始总结最近的卡片: {card_info['title']}")
                try:
                    # 处理卡片消息
                    await self._handle_card_message(bot, chat_id, card_info)
                    # 总结后删除该卡片
                    del self.recent_cards[chat_id]
                    return False
                except Exception as e:
                    logger.error(f"处理卡片时出错: {e}")
                    await bot.send_text_message(chat_id, "❌ 抱歉，处理过程中出现错误")
                    return False

            # 没有最近的URL或卡片
            else:
                # 检查消息中是否包含URL
                urls = re.findall(self.URL_PATTERN, content)
                if urls:
                    url = urls[0]
                    logger.info(f"在总结命令中找到URL: {url}")
                    if self._check_url(url):
                        try:
                            await bot.send_text_message(chat_id, "🔍 正在为您生成内容总结，请稍候...")
                            summary = await self._process_url(url)
                            if summary:
                                await bot.send_text_message(chat_id, f"🎯 内容总结如下：\n\n{summary}")
                                return False
                            else:
                                await bot.send_text_message(chat_id, "❌ 抱歉，生成总结失败")
                                return False
                        except Exception as e:
                            logger.error(f"处理URL时出错: {e}")
                            await bot.send_text_message(chat_id, "❌ 抱歉，处理过程中出现错误")
                            return False
                else:
                    await bot.send_text_message(chat_id, "❌ 没有找到可以总结的链接或卡片，请先发送链接或卡片，然后再发送总结命令")
                    return False

        # 如果不是总结命令，检查是否包含URL
        urls = re.findall(self.URL_PATTERN, content)
        if urls:
            url = urls[0]
            logger.info(f"找到URL: {url}")
            if self._check_url(url):
                # 存储URL供后续使用
                self.recent_urls[chat_id] = {
                    "url": url,
                    "timestamp": time.time()
                }
                logger.info(f"已存储URL: {url} 供后续总结使用")
                await bot.send_text_message(chat_id, "🔗 检测到链接，发送\"总结\"命令可以生成内容总结")

        return True

    @on_article_message(priority=50)
    async def handle_article_message(self, bot: 'WechatAPIClient', message: Dict) -> bool:
        """处理文章类型消息（微信公众号文章等）"""
        if not self.dify_enable:
            return True

        chat_id = message.get("FromWxid", "")
        msg_id = message.get("MsgId", "")
        logger.info(f"收到文章消息: MsgId={msg_id}, chat_id={chat_id}")

        try:
            # 处理XML消息
            card_info = self._process_xml_message(message)
            if not card_info:
                logger.warning("文章消息解析失败")
                return True

            logger.info(f"识别为文章消息: {card_info['title']}")

            # 存储卡片信息供后续使用
            self.recent_cards[chat_id] = {
                "info": card_info,
                "timestamp": time.time()
            }
            logger.info(f"已存储文章信息: {card_info['title']} 供后续总结使用")
            await bot.send_text_message(chat_id, "📰 检测到文章，发送\"总结\"命令可以生成内容总结")

            return True
        except Exception as e:
            logger.error(f"处理文章消息时出错: {e}")
            logger.exception(e)
            return True

    @on_file_message(priority=50)
    async def handle_file_message(self, bot: 'WechatAPIClient', message: Dict) -> bool:
        """处理文件类型消息（包括卡片消息）"""
        if not self.dify_enable:
            return True

        chat_id = message.get("FromWxid", "")
        msg_type = message.get("MsgType", 0)

        # 检查是否是卡片消息（类型49）
        if msg_type != 49:
            logger.info(f"非卡片消息，跳过处理: MsgType={msg_type}")
            return True

        logger.info(f"收到卡片消息: MsgType={msg_type}, chat_id={chat_id}")

        try:
            # 处理XML消息
            card_info = self._process_xml_message(message)
            if not card_info:
                logger.warning("卡片消息解析失败")
                return True

            logger.info(f"识别为卡片消息: {card_info['title']}")

            # 存储卡片信息供后续使用
            self.recent_cards[chat_id] = {
                "info": card_info,
                "timestamp": time.time()
            }
            logger.info(f"已存储卡片信息: {card_info['title']} 供后续总结使用")
            await bot.send_text_message(chat_id, "📎 检测到卡片，发送\"总结\"命令可以生成内容总结")

            return True
        except Exception as e:
            logger.error(f"处理文件消息时出错: {e}")
            logger.exception(e)
            return True