# encoding:utf-8
import json
import os
import html
from urllib.parse import urlparse

import requests
import io
import plugins
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from plugins import *
import re
import time

@plugins.register(
    name="JinaSum",
    desire_priority=10,
    hidden=False,
    enabled=False,
    desc="Sum url link content with jina reader and llm",
    version="0.0.9",
    author="hanfangyuan",
)
class JinaSum(Plugin):
    jina_reader_base = "https://r.jina.ai"
    open_ai_api_base = "https://api.openai.com/v1"
    open_ai_model = "gpt-4o-mini"
    max_words = 8000
    prompt = "请总结下面引号内的文档内容。\n\n"
    white_url_list = []
    black_url_list = [
        "https://support.weixin.qq.com",  # 视频号视频
        "https://channels-aladin.wxqcloud.qq.com",  # 视频号音乐
    ]
    generate_image = True

    def __init__(self):
        super().__init__()
        try:
            self.config = super().load_config()
            if not self.config:
                self.config = self._load_config_template()
            self.jina_reader_base = self.config.get("jina_reader_base", self.jina_reader_base)
            self.open_ai_api_base = self.config.get("open_ai_api_base", self.open_ai_api_base)
            self.open_ai_api_key = self.config.get("open_ai_api_key", "")
            self.open_ai_model = self.config.get("open_ai_model", self.open_ai_model)
            self.max_words = self.config.get("max_words", self.max_words)
            self.prompt = self.config.get("prompt", self.prompt)
            self.white_url_list = self.config.get("white_url_list", self.white_url_list)
            self.black_url_list = self.config.get("black_url_list", self.black_url_list)
            self.generate_image = self.config.get("generate_image", True)
            self.black_group_list = self.config.get("black_group_list", "")
            logger.info(f"[JinaSum] inited, config={self.config}")
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        except Exception as e:
            logger.error(f"[JinaSum] 初始化异常：{e}")
            raise "[JinaSum] init failed, ignore "

    def on_handle_context(self, e_context: EventContext, retry_count: int = 0):
        try:
            context = e_context["context"]
            content = context.content
            if context.get("isgroup", True):
                msg:ChatMessage = e_context['context']['msg']
                if msg.from_user_nickname in self.black_group_list:
                    logger.debug(f"[JinaSum] {msg.from_user_nickname} is in black group list, skip")
                    return

            if context.type != ContextType.SHARING and context.type != ContextType.TEXT:
                return
            if not self._check_url(content):
                logger.debug(f"[JinaSum] {content} is not a valid url, skip")
                return
            target_url = html.unescape(content)  # 解决公众号卡片链接校验问题

            jina_url = self._get_jina_url(target_url)
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
            response = requests.get(jina_url, headers=headers, timeout=60)
            response.raise_for_status()
            target_url_content = response.text

            openai_chat_url = self._get_openai_chat_url()
            openai_headers = self._get_openai_headers()
            openai_payload = self._get_openai_payload(target_url_content)
            logger.debug(f"[JinaSum] openai_chat_url: {openai_chat_url}, openai_headers: {openai_headers}, openai_payload: {openai_payload}")
            
            response = requests.post(openai_chat_url, headers={**openai_headers, **headers}, json=openai_payload, timeout=60)
            response.raise_for_status()
            result = response.json()['choices'][0]['message']['content']
            logger.info(f"[JinaSum] LLM原始返回内容：\n{result}")
            
            try:
                 # 尝试解析JSON
                summary_data = self._parse_json_with_fallback(result)
                if summary_data:
                    # 合并Summary和Tags
                    summary = summary_data.get('Content', {}).get('Summary', '暂无总结')
                    keypoints = summary_data.get('Content', {}).get('Keypoints', [])
                    tags = summary_data.get('Content', {}).get('Tags', '无标签')
                    title = summary_data.get('Title', "无标题")
                    author = summary_data.get('Author', "未知作者")
                    date = summary_data.get('Date', str(time.strftime("%Y-%m-%d", time.localtime())))
                    
                    # 将关键要点转换为字符串
                    keypoints_str = "\n".join([f"{i+1}. {point}" for i, point in enumerate(keypoints)])
                    
                    summary_content = f"{summary}\n\n{keypoints_str}\n\n🏷 {tags}"
                    
                    if self.generate_image:
                        image_content = self._save_summary_as_image(
                            summary_content=summary_content,
                            date=f"{date}日",
                            title=title,
                            author=author
                        )
                        if image_content:
                            image_storage = io.BytesIO(image_content)
                            reply = Reply(ReplyType.IMAGE, image_storage)
                        else:
                            reply = Reply(ReplyType.ERROR, "生成图片总结失败")
                    else:
                         reply = Reply(ReplyType.TEXT, summary_content)
                else:
                   reply = Reply(ReplyType.ERROR, "解析总结内容失败，请检查LLM输出")
            except Exception as e:
                logger.error(f"[JinaSum] 处理总结内容失败：{str(e)}")
                reply = Reply(ReplyType.ERROR, "处理总结内容失败，请重试")

            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

        except Exception as e:
            if retry_count < 3:
                logger.warning(f"[JinaSum] {str(e)}, retry {retry_count + 1}")
                self.on_handle_context(e_context, retry_count + 1)
                return

            logger.exception(f"[JinaSum] {str(e)}")
            reply = Reply(ReplyType.ERROR, "我暂时无法总结链接，请稍后再试")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

    def get_help_text(self, verbose, **kwargs):
        return f'使用Jina Reader抓取页面内容，并使用LLM总结网页链接内容，并可以生成图片总结。'

    def _load_config_template(self):
        logger.debug("No Suno plugin config.json, use plugins/jina_sum/config.json.template")
        try:
            plugin_config_path = os.path.join(self.path, "config.json.template")
            if os.path.exists(plugin_config_path):
                with open(plugin_config_path, "r", encoding="utf-8") as f:
                    plugin_conf = json.load(f)
                    return plugin_conf
        except Exception as e:
            logger.exception(e)

    def _get_jina_url(self, target_url):
        return self.jina_reader_base + "/" + target_url

    def _get_openai_chat_url(self):
        return self.open_ai_api_base + "/chat/completions"

    def _get_openai_headers(self):
         return {
             'Authorization': f"Bearer {self.open_ai_api_key}",
             'Host': urlparse(self.open_ai_api_base).netloc
        }

    def _get_openai_payload(self, target_url_content):
        target_url_content = target_url_content[:self.max_words]
        sum_prompt = f"{self.prompt}\n\n'''{target_url_content}'''"
        messages = [{"role": "user", "content": sum_prompt}]
       
        payload = {
             'model': self.open_ai_model,
             'messages': messages
        }
        
        # 使用正则表达式检查模型是否以 "gpt" 开头且不是 "gpt-4o-mini"
        if re.match(r'^gpt', self.open_ai_model) and self.open_ai_model != 'gpt-4o-mini':
           payload['response_format'] = {"type": "json_object"}
        return payload
    
    def _parse_json_with_fallback(self, text):
        """
        尝试解析JSON，如果失败则使用正则表达式提取关键信息
        """
        def clean_text(text):
            return re.sub(r'\*\*','',text) if text else text
            
        try:
            # 去除 ```json 和 ``` 等标记
            text = re.sub(r"```(json)?\s*", "", text, flags=re.IGNORECASE)
            text = text.strip()
            # 尝试去除不可见字符
            text = "".join(ch for ch in text if ch.isprintable())
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("[JinaSum] JSON解析失败，尝试使用更健壮的正则表达式提取")
            try:
                # 使用正则表达式提取 Summary, Tags, Title, Author
                summary_match = re.search(r"(?:Summary:\s*(.+?)(?:\n\n|$))|(?:\"Summary\":\s*\"(.+?)\"(?:,|})|(?<=\"Content\":\s*{.*\"Summary\":\s*\"(.+?)\"))", text, re.DOTALL)
                tags_match = re.search(r"(?:Tags:\s*(.+?)(?:\n|$))|(?:\"Tags\":\s*\"(.+?)\"(?:,|})|(?<=\"Content\":\s*{.*\"Tags\":\s*\"(.+?)\"))", text, re.DOTALL)
                title_match = re.search(r"(?:Title:\s*(.+?)(?:\n|$))|(?:\"Title\":\s*\"(.+?)\"(?:,|})|(?<=\"Title\":\s*\"(.+?)\"))", text, re.DOTALL)
                author_match = re.search(r"(?:Author:\s*(.+?)(?:\n|$))|(?:\"Author\":\s*\"(.+?)\"(?:,|})|(?<=\"Author\":\s*\"(.+?)\"))", text, re.DOTALL)
                keypoints_match = re.findall(r'(?:(?:\d+\.\s*([^\n]+))|(?<=\"Keypoints\":\s*\[)(?:\\?"([^\\"]+)\\?"(?:,|\s*\])))', text,re.DOTALL)


                summary = clean_text((summary_match.group(1) or summary_match.group(2) or summary_match.group(3) or "暂无总结").strip()) if summary_match else "暂无总结"
                tags = clean_text((tags_match.group(1) or tags_match.group(2) or tags_match.group(3) or "无标签").strip()) if tags_match else "无标签"
                title = clean_text((title_match.group(1) or title_match.group(2) or title_match.group(3) or "无标题").strip()) if title_match else "无标题"
                author = clean_text((author_match.group(1) or author_match.group(2) or author_match.group(3) or "未知作者").strip()) if author_match else "未知作者"

                 # 提取关键要点
                keypoints = [clean_text(point.strip()) for match in keypoints_match for point in match if point]



                 # 构建返回的字典
                extracted_data = {
                  "Content": {
                        "Summary": summary,
                        "Keypoints": keypoints,
                        "Tags": tags
                    },
                   "Title":title,
                   "Author":author,
                   "Date":str(time.strftime("%Y-%m-%d", time.localtime()))
                }

                return extracted_data


            except Exception as e:
                logger.error(f"[JinaSum] 正则表达式提取失败: {e}")
                return None

    def _check_url(self, target_url: str):
        stripped_url = target_url.strip()
        if not stripped_url.startswith("http://") and not stripped_url.startswith("https://"):
            return False

        if len(self.white_url_list):
            if not any(stripped_url.startswith(white_url) for white_url in self.white_url_list):
                return False

        for black_url in self.black_url_list:
            if stripped_url.startswith(black_url):
                return False
        return True

    def _save_summary_as_image(self, summary_content, date=None, title=None, author=None):
        """将总结内容转换为图片"""
        try:
            api_url = "https://fireflycard-api.302ai.cn/api/saveImg"
            data = {
                "icon": "https://mrxc-1300093961.cos.ap-shanghai.myqcloud.com/2024/12/8/1865676194712899585.png",
                "date": date or str(time.strftime("%Y-%m-%d", time.localtime())),
                "title": title or "📝 内容总结",
                "author": author or "AI助手",
                "content": summary_content,
                "font": "Noto Sans SC",
                "fontStyle": "Regular",
                "titleFontSize": 36,
                "contentFontSize": 28,
                "contentLineHeight": 44,
                "contentColor": "#333333",
                "backgroundColor": "#FFFFFF",
                "width": 440,
                "height": 0,
                "useFont": "MiSans-Thin",
                "fontScale": 0.7,
                "ratio": "Auto",
                "padding": 15,
                "watermark": "蓝胖子速递",
                "qrCodeTitle": "<p>蓝胖子速递</p>",
                "qrCode": "https://u.wechat.com/MLCKhcLlexXLmy3Jp3FM9QE",
                "watermarkText": "",
                "watermarkColor": "#999999",
                "watermarkSize": 24,
                "watermarkGap": 20,
                "exportType": "png",
                "exportQuality": 100
            }
            response = requests.post(api_url, json=data, timeout=30)
            response.raise_for_status()
            if response.headers.get('content-type', '').startswith('image/'):
                logger.info("[JinaSum] 成功生成图片")
                return response.content
            logger.error("[JinaSum] 生成图片失败：响应格式错误")
            return None
        except Exception as e:
            logger.error(f"[JinaSum] 生成图片失败：{str(e)}")
            return None
