"""描述:
主要功能:
    - 提供基于 Embedding 的技能排序与词法回退路由能力。
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from nanobot.agent.skill_runtime.spec_schema import SkillSpec
from nanobot.config.schema import ProviderConfig

_SILICONFLOW_EMBEDDING_BASE = "https://api.siliconflow.cn/v1"
_TOKEN_RE = re.compile(r"\w+")


#region 本地存活期缓存

@dataclass(slots=True)
class _CacheEntry:
    """
    用处: 封装置于字典内的缓存节点。

    功能:
        - 记录数据体本身及该项未来的确切失联下线时刻。
    """
    expires_at: float
    value: list[float]


class _TTLVectorCache:
    """
    用处: 具备存活时效约束的轻量本地词向量存取系统。

    功能:
        - 按配置时间阻断旧数据的提取并顺手完成剔除。
    """

    def __init__(self, ttl_seconds: int, now_fn: Callable[[], float] = time.time):
        """
        用处: 置备时钟及底表容器。参数 ttl_seconds: 最大寿命周期。

        功能:
            - 初始化带有生命边界机制的浮点序列缓存池。
        """
        self._ttl_seconds = max(ttl_seconds, 0)
        self._now_fn = now_fn
        self._entries: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> list[float] | None:
        """
        用处: 以密钥查收当前生命周期体征合理的项目。参数 key: 主键。

        功能:
            - 判断并回收超时数据残片，安全转交活体存储序列。
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at < self._now_fn():
            self._entries.pop(key, None)
            return None
        return entry.value

    def set(self, key: str, value: list[float]) -> None:
        """
        用处: 将鲜活的数据并同未来死限标记一起烙印进字典。参数 key: 锚点指引，value: 具体大模型反馈向量。

        功能:
            - 设置有效期并执行推入落地动作。
        """
        if self._ttl_seconds == 0:
            return
        expires_at = self._now_fn() + self._ttl_seconds
        self._entries[key] = _CacheEntry(expires_at=expires_at, value=value)

#endregion

#region 向量路由器主体

class EmbeddingSkillRouter:
    """
    用处: 凭借语义相近度去推介指令下达动作方案的核心智能探路类。

    功能:
        - 能在外部条件就绪时执行网络端模型推理并在不满足之际弹性切出使用基于词频的文本余弦相似比较。
    """

    def __init__(
        self,
        *,
        embedding_enabled: bool = False,
        embedding_top_k: int = 3,
        embedding_model: str = "",
        embedding_timeout_seconds: int = 10,
        embedding_cache_ttl_seconds: int = 600,
        provider_config: ProviderConfig | None = None,
        http_client_factory: Callable[..., httpx.Client] = httpx.Client,
        now_fn: Callable[[], float] = time.time,
    ):
        """
        用处: 构造大模型连接器、重连阀控、及底层缓存依赖实例等设施。

        功能:
            - 配置远程模型路由服务的网络通道指引、缓存及超期防爆系数。
        """
        self.embedding_enabled = embedding_enabled
        self.embedding_top_k = max(1, embedding_top_k)
        self.embedding_model = embedding_model.strip()
        self.embedding_timeout_seconds = max(1, embedding_timeout_seconds)
        self.provider_config = provider_config or ProviderConfig()
        self._http_client_factory = http_client_factory
        self._query_cache = _TTLVectorCache(embedding_cache_ttl_seconds, now_fn=now_fn)
        self._index_cache = _TTLVectorCache(embedding_cache_ttl_seconds, now_fn=now_fn)

    def rank(self, query: str, specs: dict[str, SkillSpec]) -> list[tuple[str, float]]:
        """
        用处: 输入一段长话及靶向范围，求导最佳候选列表。参数 query: 源头问卷，specs: 所有技能设定。

        功能:
            - 判断环境可用后，组建向量阵列下发比较测算；如环境崩塌断路则采用常规词法结构作为安全降级。
        """
        normalized_query = query.strip()
        if not normalized_query or not specs:
            return []

        docs = {skill_id: self._build_index_text(skill_id, spec) for skill_id, spec in specs.items()}
        if self._embedding_ready():
            try:
                return self._rank_by_embeddings(normalized_query, docs)
            except Exception:
                pass
        return self._rank_by_lexical(normalized_query, docs)

    def _embedding_ready(self) -> bool:
        """
        用处: 自检运行期开启远端调用状态合法性。

        功能:
            - 核验设置档项中有无提供秘钥等重要接插件材料。
        """
        return bool(
            self.embedding_enabled
            and self.embedding_model
            and self.provider_config.api_key
        )

    def _rank_by_embeddings(self, query: str, docs: dict[str, str]) -> list[tuple[str, float]]:
        """
        用处: 进行跨域多方量化运算排序制高点提取。参数 query: 当场文段，docs: 目标说明书族群。

        功能:
            - 发出 HTTP 到远边获取模型数字点位投影后再和缓存对比查缺填补，执行所有投影距离内积计算最终推选顶部序列。
        """
        query_key = self._query_key(query)
        query_vector = self._query_cache.get(query_key)
        if query_vector is None:
            query_vector = self._embed_texts([query])[0]
            self._query_cache.set(query_key, query_vector)

        skill_ids = sorted(docs)
        vectors: dict[str, list[float]] = {}
        missing_ids: list[str] = []
        missing_texts: list[str] = []

        for skill_id in skill_ids:
            index_key = self._index_key(skill_id, docs[skill_id])
            cached = self._index_cache.get(index_key)
            if cached is None:
                missing_ids.append(skill_id)
                missing_texts.append(docs[skill_id])
            else:
                vectors[skill_id] = cached

        if missing_texts:
            embedded = self._embed_texts(missing_texts)
            for skill_id, vector in zip(missing_ids, embedded, strict=False):
                vectors[skill_id] = vector
                self._index_cache.set(self._index_key(skill_id, docs[skill_id]), vector)

        scored = [
            (skill_id, self._cosine_similarity(query_vector, vectors[skill_id]))
            for skill_id in skill_ids
        ]
        return self._top_k(scored)

    def _rank_by_lexical(self, query: str, docs: dict[str, str]) -> list[tuple[str, float]]:
        """
        用处: 退化版非依赖性模型匹配引擎。参数 query: 短句，docs: 背景文献集。

        功能:
            - 借助原生字典计算标记的分布量构建基础余弦夹角计算比对出重合度高地分。
        """
        query_tokens = Counter(self._tokenize(query))
        scored = [
            (skill_id, self._token_overlap_score(query_tokens, Counter(self._tokenize(text))))
            for skill_id, text in docs.items()
        ]
        return self._top_k(scored)

    def _embed_texts(self, inputs: list[str]) -> list[list[float]]:
        """
        用处: 与指定大模型底座实操交手调用产生浮点多维组合。参数 inputs: 多段文案列。

        功能:
            - 装载安全凭证包装 HTTP 主体抛到服务终点去换取转换完全的向量回应包并摘出主体。
        """
        payload = {
            "model": self.embedding_model,
            "input": inputs,
        }
        url = self._embeddings_url()
        headers = {"Authorization": f"Bearer {self.provider_config.api_key}"}
        if self.provider_config.extra_headers:
            headers.update(self.provider_config.extra_headers)

        with self._http_client_factory(timeout=self.embedding_timeout_seconds) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()

        body = response.json()
        raw_data = body.get("data")
        if not isinstance(raw_data, list) or len(raw_data) != len(inputs):
            raise ValueError("unexpected embedding response shape")

        indexed = sorted(raw_data, key=lambda item: int(item.get("index", 0)))
        vectors: list[list[float]] = []
        for item in indexed:
            embedding = item.get("embedding")
            if not isinstance(embedding, list):
                raise ValueError("missing embedding vector")
            vectors.append([float(v) for v in embedding])
        return vectors

    def _build_index_text(self, skill_id: str, spec: SkillSpec) -> str:
        """
        用处: 压缩组合形成供检索用文档素材底本。参数 spec: 某单一设定的字典树结构。

        功能:
            - 把表内关键字段提取包含主副标释义以及演示范例压平成一张大文本。
        """
        meta = spec.meta.model_dump()
        parts = [skill_id]
        for key in ("description", "match_description"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())

        for key in ("examples", "example"):
            value = meta.get(key)
            parts.extend(self._flatten_examples(value))

        return "\n".join(parts)

    def _flatten_examples(self, value: Any) -> list[str]:
        """
        用处: 深层次扁平化嵌套的多样例节点。参数 value: 原样本块。

        功能:
            - 实施递归提取扫清字典套或者列嵌套形成的阻碍汇入列表基体。
        """
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, list):
            results: list[str] = []
            for item in value:
                results.extend(self._flatten_examples(item))
            return results
        if isinstance(value, dict):
            results: list[str] = []
            for item in value.values():
                results.extend(self._flatten_examples(item))
            return results
        return []

    def _tokenize(self, text: str) -> list[str]:
        """
        用处: 原生字符分割截除法组件。参数 text: 行文语。

        功能:
            - 借助基本通盘字符正交降阶分离获取微元化字符包。
        """
        return [token.lower() for token in _TOKEN_RE.findall(text)]

    def _token_overlap_score(self, query: Counter[str], doc: Counter[str]) -> float:
        """
        用处: 针对字元的内积算法函数。参数 query: 请求项频率统计，doc: 被查询端统计。

        功能:
            - 通过代数公式推导基础文本覆盖共用规模并定段于区间小数内。
        """
        if not query or not doc:
            return 0.0
        dot = sum(query[token] * doc[token] for token in query if token in doc)
        if dot == 0:
            return 0.0
        query_norm = math.sqrt(sum(v * v for v in query.values()))
        doc_norm = math.sqrt(sum(v * v for v in doc.values()))
        return dot / (query_norm * doc_norm) if query_norm and doc_norm else 0.0

    def _cosine_similarity(self, left: list[float], right: list[float]) -> float:
        """
        用处: 标准的多维数值向列余弦值统核方法。参数 left: 数组一，right: 对手数组二。

        功能:
            - 计算在高维度下两数组贴近空间角度距离以表示语义方向上的相关接近值。
        """
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right, strict=False))
        left_norm = math.sqrt(sum(v * v for v in left))
        right_norm = math.sqrt(sum(v * v for v in right))
        return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0

    def _top_k(self, scored: list[tuple[str, float]]) -> list[tuple[str, float]]:
        """
        用处: 将排好的计分卡片施加设定的门槛修剪。参数 scored: 全部成果集合。

        功能:
            - 处理总榜按分数排前排后切割多余低质成员并抛出精准控制限额集。
        """
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[: self.embedding_top_k]

    def _query_key(self, query: str) -> str:
        """
        用处: 下游针对长句请求的缓存存储钥制造厂。参数 query: 用户文本。

        功能:
            - 利用当前运载底座型号进行结合以避不同预训练模型污染撞库。
        """
        return f"{self.embedding_model}::q::{query}"

    def _index_key(self, skill_id: str, text: str) -> str:
        """
        用处: 索引项向后台落库的键名组合函数。参数 skill_id: 技能主体别名，text: 内容指纹。

        功能:
            - 防止单一字典内的值交叉覆写，提供明确分明结构线索。
        """
        return f"{self.embedding_model}::i::{skill_id}::{text}"

    def _embeddings_url(self) -> str:
        """
        用处: 连接组装器获取最后向大模型下发材料的目的地地址。

        功能:
            - 控制及矫正由配置内传入的主基底并贴合访问路径的最后一段路由。
        """
        base = (self.provider_config.api_base or _SILICONFLOW_EMBEDDING_BASE).rstrip("/")
        return f"{base}/embeddings"

#endregion
