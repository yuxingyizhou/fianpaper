"""LLM 摘要模块（论文版） - DeepSeek / MiniMax (HTTP 直连)。

相对 my2 的差异：
- 默认 prompt 替换为论文版（含推荐理由 / 适合谁读 / 推荐等级）。
- 渲染时支持 ``{authors}`` / ``{journal}`` 占位符（PaperArticle 才有），
  非 PaperArticle 用空字符串兜底，模板不要求一定使用这两个字段。
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

import requests

from .article import Article, PaperArticle
from .log_utils import get_logger

logger = get_logger(__name__)


MIN_SUMMARY_LENGTH = 60
MIN_CONTENT_LENGTH = 20
REQUIRED_MARKERS = 3
EXPECTED_MARKERS = frozenset({'🏷️', '📌', '💡', '📋', '⭐'})

_THINK_BLOCK_RE = re.compile(r'<think>.*?</think>\s*', re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r'<think>', re.IGNORECASE)
_CLOSE_THINK_RE = re.compile(r'</think>\s*', re.IGNORECASE)


def _strip_thinking(text: str) -> str:
    """剥离推理模型的 <think>...</think> 思维链块。"""
    if not text:
        return text
    text = _THINK_BLOCK_RE.sub('', text)
    m = _CLOSE_THINK_RE.search(text)
    if m:
        text = text[m.end():]
    if _OPEN_THINK_RE.search(text):
        return ''
    return text.strip()


def _build_reflection_prompt(issue: str, format_spec: str) -> str:
    return (
        f"你上一次的输出**不合格**，原因：**{issue}**\n\n"
        "请基于这条反馈，**重新输出修正后的摘要**。\n\n"
        "## 再次强调原始格式要求（务必逐字符遵守）\n\n"
        f"{format_spec}\n\n"
        "## 额外注意\n"
        "1. 必须以 🏷️ 开头，依次包含 🏷️ 📌 💡 📋 ⭐ 五个标记\n"
        "2. 不要任何 <think> 思考块、解释、道歉、开场白或结束语\n"
        "3. 不要保留方括号占位符（如 [中文标题翻译]），要换成真实内容\n"
        "4. 不要英文原文、不要重复本次反馈、不要重复原文章标题\n"
        "5. 直接输出最终结构化内容"
    )


REJECTION_PATTERNS = [
    "i cannot", "i'm unable", "i am unable",
    "cannot summarize", "cannot provide", "cannot generate",
    "无法总结", "无法提供", "无法生成", "我无法", "抱歉，我无法",
]

BOILERPLATE_PATTERNS = [
    "please enable javascript",
    "enable cookies",
    "javascript is required",
    "请启用javascript",
]


def _is_content_valid(content: str) -> bool:
    text = content.strip()
    if len(text) < MIN_CONTENT_LENGTH:
        return False
    lower = text.lower()
    for pattern in BOILERPLATE_PATTERNS:
        if pattern in lower:
            return False
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if lines:
        url_lines = sum(1 for l in lines if l.startswith('http'))
        if url_lines / len(lines) > 0.8:
            return False
    return True


def _is_summary_valid(summary: str) -> Optional[str]:
    text = summary.strip()
    if len(text) < MIN_SUMMARY_LENGTH:
        return f"过短 ({len(text)} 字 < {MIN_SUMMARY_LENGTH})"
    if not text.startswith('🏷️'):
        return f"未以 🏷️ 开头（前30字: {text[:30]!r}）"
    lower = text.lower()
    for pattern in REJECTION_PATTERNS:
        if pattern in lower:
            return f"通用拒绝回复 (匹配: '{pattern}')"
    marker_count = sum(1 for m in EXPECTED_MARKERS if m in text)
    if marker_count < REQUIRED_MARKERS:
        return f"格式不完整 (标记数: {marker_count}/{len(EXPECTED_MARKERS)}, 要求 ≥{REQUIRED_MARKERS})"
    return None


class _OpenAICompatibleProvider:
    def __init__(self, model: str, api_key: str, base_url: str, timeout: int):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    def invoke(self, messages: List[dict], max_tokens: int) -> str:
        resp = requests.post(
            f"{self.base_url}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"] or ""


class _DeepSeekProvider(_OpenAICompatibleProvider):
    def __init__(self, config: dict, timeout: int):
        super().__init__(
            model=config.get('model', 'deepseek-chat'),
            api_key=config.get('api_key', ''),
            base_url=config.get('base_url', 'https://api.deepseek.com'),
            timeout=timeout,
        )


class _MiniMaxProvider(_OpenAICompatibleProvider):
    def __init__(self, config: dict, timeout: int):
        super().__init__(
            model=config.get('model', 'minimax-text-2.7'),
            api_key=config.get('api_key', ''),
            base_url=config.get('base_url', 'https://api.minimax.chat'),
            timeout=timeout,
        )


_PROVIDERS = {
    'deepseek': _DeepSeekProvider,
    'minimax': _MiniMaxProvider,
}


_PAPER_DEFAULT_PROMPT = """请用中文为以下学术论文生成结构化摘要：

原标题: {title}
作者: {authors}
期刊/来源: {journal}
摘要原文: {content}

请严格按以下格式输出（不要添加其他内容）：

🏷️ [中文标题翻译]

📌 [一句话核心贡献，不超过30字]

💡 推荐理由:
[1-2句说明为什么值得关注——方法新颖性、数据规模、综述完整性、与某热点的相关性等]

📋 关键要点:
• 解决的问题: ...
• 采用的方法: ...
• 主要结论: ...

👥 适合: [领域研究者 / 跨学科入门 / 工程实践参考]

⭐ 推荐: [必读 / 选读 / 速览]
"""


class Summarizer:
    def __init__(self, config: dict):
        provider_name = config.get('provider', 'deepseek')
        provider_cls = _PROVIDERS.get(provider_name)
        if provider_cls is None:
            raise ValueError(
                f"未知的 LLM provider: {provider_name}，可用: {list(_PROVIDERS.keys())}"
            )
        provider_cfg = config.get(provider_name, {})
        self.max_retries = config.get('max_retries', 2)
        self.max_tokens = config.get('max_tokens', 700)
        self.max_workers = config.get('max_workers', 3)
        self.prompt_template = config.get('summary_prompt', _PAPER_DEFAULT_PROMPT)
        self._format_spec = self._extract_format_spec(self.prompt_template)
        self._provider = provider_cls(provider_cfg, config.get('timeout', 30))

    @staticmethod
    def _extract_format_spec(template: str) -> str:
        markers = (
            '请严格按以下格式输出',
            '严格按以下格式输出',
            '请按以下格式输出',
            '按以下格式输出',
        )
        for marker in markers:
            idx = template.find(marker)
            if idx >= 0:
                return template[idx:].strip()
        return template.strip()

    @staticmethod
    def _render_prompt(template: str, article: Article) -> str:
        """以字典 format 渲染，缺失字段（authors/journal 等）兜底为空串。"""
        authors_str = ""
        journal_str = ""
        if isinstance(article, PaperArticle):
            if article.authors:
                head = ", ".join(article.authors[:6])
                if len(article.authors) > 6:
                    head += " et al."
                authors_str = head
            journal_str = article.journal or ""
        fields = {
            "title": article.title or "",
            "content": article.content or "",
            "authors": authors_str,
            "journal": journal_str,
        }

        class _DefaultDict(dict):
            def __missing__(self, key):  # noqa: ANN001
                return ""

        return template.format_map(_DefaultDict(fields))

    def summarize(self, article: Article) -> Optional[str]:
        if not _is_content_valid(article.content):
            logger.warning(f"  [跳过] 内容质量不足 ({article.title[:40]}...)")
            return None

        prompt = self._render_prompt(self.prompt_template, article)
        messages = [{"role": "user", "content": prompt}]
        title_short = article.title[:40]

        last_error = None
        for attempt in range(1 + self.max_retries):
            try:
                raw = self._provider.invoke(messages, self.max_tokens)
            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    wait = 2 ** attempt
                    logger.warning(
                        f"  [重试 {attempt + 1}/{self.max_retries}] ({title_short}...) "
                        f"网络错误，等待 {wait}s: {e}"
                    )
                    time.sleep(wait)
                continue

            cleaned = _strip_thinking(raw)
            if not cleaned:
                issue = "你的回答中只有 <think> 思考块，没有任何最终的格式化摘要内容。"
            else:
                issue = _is_summary_valid(cleaned)

            if issue is None:
                return cleaned

            last_error = ValueError(f"摘要质量不合格: {issue}")
            logger.warning(
                f"  [反思 {attempt + 1}/{self.max_retries}] ({title_short}...) {issue}"
            )
            if attempt < self.max_retries:
                visible = cleaned if cleaned else raw[:800]
                messages.append({"role": "assistant", "content": visible})
                messages.append({
                    "role": "user",
                    "content": _build_reflection_prompt(issue, self._format_spec),
                })

        logger.error(f"[错误] LLM 摘要失败 ({title_short}...): {last_error}")
        return None

    def summarize_batch(
        self,
        articles: List[Article],
    ) -> List[Tuple[Article, Optional[str]]]:
        if not articles:
            return []
        results = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self.summarize, article): article
                for article in articles
            }
            for future in as_completed(futures):
                article = futures[future]
                try:
                    summary = future.result()
                except Exception as e:
                    logger.error(
                        f"[错误] 并发摘要异常 ({article.title[:40]}...): {e}"
                    )
                    summary = None
                results[article.url_hash] = (article, summary)
        return [results[a.url_hash] for a in articles]
