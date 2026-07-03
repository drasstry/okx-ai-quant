import json
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from okx_ai_quant.config import Settings
from okx_ai_quant.models import Signal
from okx_ai_quant.risk import RiskDecision


@dataclass(frozen=True, kw_only=True)
class OpenAICompatibleLLMClient:
    base_url: str
    api_key: str
    model: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "OpenAICompatibleLLMClient | None":
        if not settings.LLM_ENABLED or not settings.LLM_API_KEY:
            return None
        return cls(
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
            model=settings.LLM_MODEL,
        )

    def generate_trade_analysis(
        self,
        *,
        signal: Signal,
        decision: RiskDecision,
        fallback_english: str,
        fallback_chinese: str,
    ) -> tuple[str, str]:
        from openai import OpenAI

        client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        prompt = (
            "You are a cautious crypto quant trading assistant. "
            "Explain the signal and risk decision in concise English and Chinese. "
            "Do not give financial advice or guarantee profit. Return strict JSON with keys "
            "english and chinese.\n\n"
            f"Signal: {_json_ready(signal)}\n"
            f"Risk decision: {_json_ready(decision)}\n"
            f"Deterministic English fallback: {fallback_english}\n"
            f"Deterministic Chinese fallback: {fallback_chinese}"
        )
        try:
            response = client.responses.create(
                model=self.model,
                input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            )
            text = _extract_response_text(response)
        except _RESPONSES_API_UNSUPPORTED_ERRORS:
            response = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            text = _extract_chat_completion_text(response)
        parsed = json.loads(text)
        english = str(parsed.get("english", "")).strip()
        chinese = str(parsed.get("chinese", "")).strip()
        if not english or not chinese:
            raise RuntimeError("LLM response missing english/chinese fields")
        return english, chinese

    def generate_trade_overview(
        self,
        *,
        context: dict[str, Any],
        fallback_chinese: str,
    ) -> str:
        from openai import OpenAI

        client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        prompt = (
            "你是谨慎的加密货币量化交易风控助手。"
            "报告正文已经列出了账户数字、持仓和平仓明细，不要复述这些数字。"
            "请基于下面的系统交易快照，用不超过 3 句话、120 字以内给出点评，"
            "只讲最重要的内容：1) 今日盈亏的主要来源或原因；"
            "2) 当前最大的一个风险（若有失败日志或稳定性问题且影响交易，点名它）；"
            "3) 下一步最值得观察的一件事。"
            "不要给投资建议，不要承诺收益。Return strict JSON with key summary.\n\n"
            f"Context: {json.dumps(_json_ready(context), ensure_ascii=False, sort_keys=True)}\n"
            f"Deterministic fallback: {fallback_chinese}"
        )
        try:
            response = client.responses.create(
                model=self.model,
                input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            )
            text = _extract_response_text(response)
        except _RESPONSES_API_UNSUPPORTED_ERRORS:
            response = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            text = _extract_chat_completion_text(response)
        parsed = json.loads(text)
        summary = str(parsed.get("summary", "")).strip()
        if not summary:
            raise RuntimeError("LLM response missing summary field")
        return summary


def _responses_api_unsupported_errors() -> tuple[type[BaseException], ...]:
    """Return the tuple of error classes that mean "retry via chat.completions".

    We intentionally narrow this to "endpoint-not-available" style errors so
    network/auth failures are not silently amplified into a second call.
    """
    errors: list[type[BaseException]] = [AttributeError]
    try:
        import openai  # type: ignore import-not-found

        for name in ("NotFoundError", "BadRequestError", "UnsupportedAPIError"):
            candidate = getattr(openai, name, None)
            if isinstance(candidate, type) and issubclass(candidate, BaseException):
                errors.append(candidate)
    except Exception:
        pass
    return tuple(errors)


_RESPONSES_API_UNSUPPORTED_ERRORS = _responses_api_unsupported_errors()


def _extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text)

    if isinstance(response, dict):
        output_text = response.get("output_text")
        if output_text:
            return str(output_text)
        output = response.get("output", [])
    else:
        output = getattr(response, "output", [])

    chunks: list[str] = []
    for item in output or []:
        content = item.get("content", []) if isinstance(item, dict) else getattr(item, "content", [])
        for part in content or []:
            if isinstance(part, dict):
                text = part.get("text")
            else:
                text = getattr(part, "text", None)
            if text:
                chunks.append(str(text))
    if chunks:
        return "".join(chunks)
    return str(response)


def _extract_chat_completion_text(response: Any) -> str:
    if isinstance(response, dict):
        return str(response["choices"][0]["message"]["content"])
    return str(response.choices[0].message.content)


def _json_ready(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return _normalize_for_json(asdict(value))
    return _normalize_for_json(value)


def _normalize_for_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_for_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize_for_json(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
