"""LLM-backed synthesis for schema-derived table profiles."""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMProvider


class TableProfileSynthesizer:
    def __init__(self, *, provider: LLMProvider, model: str | None = None):
        self._provider = provider
        self._model = model or provider.get_default_model()

    @staticmethod
    def _compact_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for item in fields:
            if not isinstance(item, dict):
                continue
            property_payload = item.get("property") if isinstance(item.get("property"), dict) else {}
            options = property_payload.get("options") if isinstance(property_payload.get("options"), list) else []
            compact.append(
                {
                    "field_name": str(item.get("field_name") or item.get("name") or "").strip(),
                    "type": item.get("type"),
                    "options": [
                        str(opt.get("name") or "").strip()
                        for opt in options[:8]
                        if isinstance(opt, dict) and str(opt.get("name") or "").strip()
                    ],
                }
            )
        return compact

    async def synthesize(
        self,
        *,
        alias: str,
        table_name: str,
        fields: list[dict[str, Any]],
        seed_profile: dict[str, Any],
    ) -> dict[str, Any] | None:
        compact_fields = self._compact_fields(fields)
        system_prompt = (
            "You summarize Feishu Bitable schemas into compact JSON profiles for routing and write preparation. "
            "Return JSON only. Keep outputs concise, grounded in the schema, and do not invent unsupported field semantics."
        )
        user_prompt = json.dumps(
            {
                "task": "Summarize this Feishu table schema into a compact profile.",
                "requirements": {
                    "aliases": "1-6 likely user-facing aliases or paraphrases",
                    "purpose_guess": "one short sentence",
                    "common_query_patterns": "1-4 short Chinese examples",
                    "common_write_patterns": "1-4 short Chinese examples",
                    "confidence": "low|medium|high",
                },
                "table": {
                    "alias": alias,
                    "table_name": table_name,
                    "seed_profile": {
                        "display_name": seed_profile.get("display_name"),
                        "aliases": seed_profile.get("aliases"),
                        "purpose_guess": seed_profile.get("purpose_guess"),
                        "identity_fields_guess": seed_profile.get("identity_fields_guess"),
                        "person_fields": seed_profile.get("person_fields"),
                        "time_fields": seed_profile.get("time_fields"),
                        "status_fields": seed_profile.get("status_fields"),
                    },
                    "fields": compact_fields,
                },
                "output_schema": {
                    "aliases": ["string"],
                    "purpose_guess": "string",
                    "common_query_patterns": ["string"],
                    "common_write_patterns": ["string"],
                    "confidence": "low|medium|high",
                },
            },
            ensure_ascii=False,
        )
        try:
            response = await self._provider.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                tools=None,
                model=self._model,
                max_tokens=600,
                temperature=0.1,
            )
            content = str(response.content or "").strip()
            if not content:
                return None
            payload = json.loads(content)
            if not isinstance(payload, dict):
                return None
            aliases_raw = payload.get("aliases")
            aliases = [
                str(item).strip()
                for item in aliases_raw
                if isinstance(item, str) and str(item).strip()
            ] if isinstance(aliases_raw, list) else []
            query_patterns_raw = payload.get("common_query_patterns")
            common_query_patterns = [
                str(item).strip()
                for item in query_patterns_raw
                if isinstance(item, str) and str(item).strip()
            ] if isinstance(query_patterns_raw, list) else []
            write_patterns_raw = payload.get("common_write_patterns")
            common_write_patterns = [
                str(item).strip()
                for item in write_patterns_raw
                if isinstance(item, str) and str(item).strip()
            ] if isinstance(write_patterns_raw, list) else []
            confidence = str(payload.get("confidence") or "medium").strip().lower() or "medium"
            if confidence not in {"low", "medium", "high"}:
                confidence = "medium"
            result = {
                "aliases": aliases,
                "purpose_guess": str(payload.get("purpose_guess") or "").strip(),
                "common_query_patterns": common_query_patterns,
                "common_write_patterns": common_write_patterns,
                "confidence": confidence,
            }
            if not any(result.values()):
                return None
            return result
        except Exception as exc:
            logger.warning("Table profile synthesis failed for {}: {}", table_name or alias, exc)
            return None
