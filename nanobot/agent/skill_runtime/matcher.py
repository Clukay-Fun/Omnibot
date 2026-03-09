"""描述:
主要功能:
    - 根据规则与向量回退策略选择目标技能。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from nanobot.agent.skill_runtime.embedding_router import EmbeddingSkillRouter
from nanobot.agent.skill_runtime.spec_schema import SkillSpec

#region 匹配模型层

@dataclass(slots=True)
class MatchSelection:
    """
    用处: 反馈分析器最终敲定的单一决策面。

    功能:
        - 携带命中技能 ID 及其所依据的原因。
        - 剥余除去关键字外的纯参数内容，为后向逻辑补提供残余查询段。
    """
    spec_id: str
    remainder: str
    reason: str
    score: float | None = None

#endregion

#region 匹配器实现层

class SkillSpecMatcher:
    """
    用处: 主导语言与配置间的双向桥接定位工作。

    功能:
        - 内嵌了面向特定诸如 `case_search` 的查询专用清洗词典。
        - 根据阶梯级的选择规则逐次降级匹配出目标触发动作。
    """
    _CASE_SEARCH_SPEC_ID = "case_search"
    _CASE_QUERY_LIMIT_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"(?:列出|显示|返回|给我|只要|只看|前|top)\s*(\d{1,3})\s*(?:条|个|项|行|条记录|records?)", re.IGNORECASE),
        re.compile(r"(\d{1,3})\s*(?:条|个|项|行)\s*(?:就行|即可|够了|就好|给我)?", re.IGNORECASE),
    )

    def __init__(
        self,
        specs: dict[str, SkillSpec],
        *,
        embedding_router: EmbeddingSkillRouter | None = None,
        embedding_min_score: float = 0.15,
        case_query_keywords: tuple[str, ...] | None = None,
        case_query_intent_tokens: tuple[str, ...] | None = None,
        case_query_exclude_tokens: tuple[str, ...] | None = None,
        case_query_prefixes: tuple[str, ...] | None = None,
        case_query_suffixes: tuple[str, ...] | None = None,
    ):
        """
        用处: 注射核心字典及特定场景下的清洗分词数组支持。参数 specs: 全量的配置。

        功能:
            - 初始化向量路由和内置检索相关的专用词根、意图、规避词数组。
        """
        self._specs = specs
        self._embedding_router = embedding_router
        self._embedding_min_score = max(0.0, float(embedding_min_score))
        self._case_query_keywords = tuple(case_query_keywords or ("case",))
        self._case_query_intent_tokens = tuple(
            case_query_intent_tokens
            or (
                "查",
                "查下",
                "查一下",
                "查询",
                "搜索",
                "查找",
                "检索",
                "看看",
                "找",
                "找下",
                "找一下",
            )
        )
        self._case_query_exclude_tokens = tuple(
            case_query_exclude_tokens
            or (
                "代办",
                "待办",
                "清单",
                "勾选",
                "卡片",
                "记一下",
                "记录",
            )
        )
        self._case_query_prefixes = tuple(case_query_prefixes or ())
        self._case_query_suffixes = tuple(case_query_suffixes or ())

    def select(self, text: str) -> MatchSelection | None:
        """
        用处: 主入口防爆分流总函数。参数 text: 客户端用户输入的完整指令。

        功能:
            - 依次串联通过强制直通、垂直域探测、精准正则匹配以及通用文本嵌入路由四大阶段进行排查并锁定技能。
        """
        content = text.strip()
        if not content:
            return None

        explicit = self._select_explicit(content)
        if explicit:
            return explicit

        by_domain_hint = self._select_domain_hint(content)
        if by_domain_hint:
            return by_domain_hint

        by_regex = self._select_regex(content)
        if by_regex:
            return by_regex

        return self._select_by_keywords(content)

    def _select_domain_hint(self, text: str) -> MatchSelection | None:
        """
        用处: 根据专有名词指涉来预判是否走固定垂直检索技能。参数 text: 用户下发字符串样本。

        功能:
            - 针对硬编码的搜索能力预判文本结构以决定应否跳过耗时深层次比对直接派发。
        """
        case_skill = self._CASE_SEARCH_SPEC_ID
        if case_skill in self._specs and self._looks_like_case_query(text):
            return MatchSelection(
                spec_id=case_skill,
                remainder=self._extract_case_query(text),
                reason="domain_hint",
            )
        return None

    def _looks_like_case_query(self, text: str) -> bool:
        """
        用处: 意图初判断探测器。参数 text: 用户查询句。

        功能:
            - 在遇到拦截意图或没触发特征字时剔除干扰，并深度诊断残余串是否携带搜索必要关键位。
        """
        lowered = text.lower()
        if any(token and token.lower() in lowered for token in self._case_query_exclude_tokens):
            return False
        has_object = any(keyword and keyword.lower() in lowered for keyword in self._case_query_keywords)
        if not has_object:
            return False
        has_intent = any(token and token.lower() in lowered for token in self._case_query_intent_tokens)
        if not has_intent:
            return False
        return self._has_meaningful_case_query(text)

    def _has_meaningful_case_query(self, text: str) -> bool:
        """
        用处: 清污探源诊断是否具有实在意义的搜索请求。参数 text: 输入段。

        功能:
            - 将预设定指令去除再排除空值等干扰，若为空表明没有实质参数则折返否定结果。
        """
        extracted = self._extract_case_query(text)
        if not extracted:
            return False
        normalized = extracted.lower()
        for token in sorted(self._case_query_keywords, key=len, reverse=True):
            lowered = token.strip().lower()
            if lowered:
                normalized = normalized.replace(lowered, "")
        normalized = re.sub(r"[\s:：\-_/，,。！？!?的]", "", normalized)
        return bool(normalized)

    def _allow_case_search_match(self, *, spec_id: str, text: str) -> bool:
        """
        用处: 防范泛听导致专有检索误开启的守门器。参数 spec_id: 处理候选名，text: 交互文案。

        功能:
            - 判断候选的是否属于限制级检索分支并对其实行更严酷的表观预校验。
        """
        if spec_id != self._CASE_SEARCH_SPEC_ID:
            return True
        return self._looks_like_case_query(text)

    def _extract_case_query(self, text: str) -> str:
        """
        用处: 分离洗刷核心意途搜索条件。参数 text: 原始数据。

        功能:
            - 根据字典在句首和句尾剔除类似"帮忙查找"、"的数据"等修饰残渣，精辟提炼中心字块。
        """
        source = text.strip()
        limit = self._extract_case_query_limit(source)
        segment = re.split(r"[，,。！？!?\n]", source, maxsplit=1)[0].strip()
        if self._case_query_prefixes:
            prefix_pattern = "|".join(re.escape(token) for token in self._case_query_prefixes if token)
            if prefix_pattern:
                segment = re.sub(rf"^(?:{prefix_pattern})\s*", "", segment)
        if self._case_query_suffixes:
            suffix_pattern = "|".join(re.escape(token) for token in self._case_query_suffixes if token)
            if suffix_pattern:
                segment = re.sub(rf"(?:的)?(?:{suffix_pattern})\s*$", "", segment)
        segment = re.sub(r"\s+", " ", segment).strip()
        if limit is not None:
            segment = f"page_size={limit} {segment}".strip()
        return segment

    @classmethod
    def _extract_case_query_limit(cls, text: str) -> int | None:
        for pattern in cls._CASE_QUERY_LIMIT_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            try:
                limit = int(match.group(1))
            except (TypeError, ValueError):
                continue
            return max(1, limit)
        return None

    def _select_explicit(self, text: str) -> MatchSelection | None:
        """
        用处: 精准明确的长串指令（类似硬链接触发方式）捕手。参数 text: 聊天串。

        功能:
            - 解析带有 /skill 开首的调试/特定强触发形态语句直接映射命中。
        """
        match = re.match(r"^/skill\s+([a-zA-Z0-9_\-]+)\s*(.*)$", text, re.IGNORECASE)
        if not match:
            return None
        spec_id = match.group(1).strip()
        if spec_id not in self._specs:
            return None
        return MatchSelection(spec_id=spec_id, remainder=match.group(2).strip(), reason="explicit")

    def _select_regex(self, text: str) -> MatchSelection | None:
        """
        用处: 根据自定义正则表达式阵列来探索技能库。参数 text: 人工文案。

        功能:
            - 遍历在规格档案内设定的正则表达式条件只要符合就产生挂靠绑定。
        """
        for spec_id, spec in self._specs.items():
            regex = None
            if spec.meta.match and spec.meta.match.regex:
                regex = spec.meta.match.regex
            elif spec.match and spec.match.regex:
                regex = spec.match.regex
            if not regex:
                continue
            if re.search(regex, text, flags=re.IGNORECASE):
                if not self._allow_case_search_match(spec_id=spec_id, text=text):
                    continue
                return MatchSelection(spec_id=spec_id, remainder=text, reason="regex")
        return None

    def _select_by_keywords(self, text: str) -> MatchSelection | None:
        """
        用处: 散列关键字权值计分系统来进行粗筛评判器。参数 text: 清理后的用户字音。

        功能:
            - 依据名字、配置内声明的关键锚点甚至是说明文本的囊括程度进行分制积分战，得胜者晋级，反之呼唤后备。
        """
        tokens = {token for token in re.findall(r"[\w\u4e00-\u9fff]+", text.lower()) if token}
        if not tokens:
            return self._select_by_embedding(text)

        best_id: str | None = None
        best_score = 0
        for spec_id, spec in self._specs.items():
            if not self._allow_case_search_match(spec_id=spec_id, text=text):
                continue
            score = 0
            description = (spec.meta.description or "").lower()
            for token in tokens:
                if token in description:
                    score += 2

            for part in re.split(r"[_\-]", spec_id.lower()):
                if part and part in tokens:
                    score += 3

            extra_keywords: list[str] = []
            if spec.meta.match:
                extra_keywords.extend(spec.meta.match.keywords)
            if spec.match:
                extra_keywords.extend(spec.match.keywords)
            for keyword in extra_keywords:
                if keyword and keyword.lower() in text.lower():
                    score += 4

            if score > best_score:
                best_score = score
                best_id = spec_id

        if best_id and best_score > 0:
            return MatchSelection(spec_id=best_id, remainder=text, reason="keywords")

        return self._select_by_embedding(text)

    def _select_by_embedding(self, text: str) -> MatchSelection | None:
        """
        用处: 当所有字符特征探索均落败后启用的高维智能路由。参数 text: 无特征文本。

        功能:
            - 交涉底层向量引擎取得各任务技能逼近系数排行榜，取及格线以上最相符项。
        """
        if not self._embedding_router:
            return None
        ranked = self._embedding_router.rank(text, self._specs)
        if not ranked:
            return None

        selected: tuple[str, float] | None = None
        for spec_id, score in ranked:
            if spec_id not in self._specs:
                continue
            if not self._allow_case_search_match(spec_id=spec_id, text=text):
                continue
            selected = (spec_id, score)
            break
        if selected is None:
            return None

        spec_id, score = selected
        if score < self._embedding_min_score:
            return None
        return MatchSelection(spec_id=spec_id, remainder=text, reason="embedding", score=float(score))

#endregion
