import json
from pathlib import Path
from types import SimpleNamespace

from copilot1c.agent import tools as agent
from copilot1c.config import Settings
from copilot1c.retrieval import number_refs, smart_search, source_label


def test_number_refs():
    assert number_refs("Какой результат у тест-кейса 34 ПиМИ НСИ?") == [("test_case", "34")]
    assert number_refs("Что проверяется в пункте 64 плана тестирования?") == [("plan_item", "64")]
    assert number_refs("Почему не 11.6?") == []


def test_smart_search_puts_exact_number_hits_first():
    def search(q, f, k):
        if f.get("test_case") == "34":
            return [{"file_id": "tc34", "text": "Тест-кейс № 34"}]
        return [{"file_id": "x", "text": "похожее"}, {"file_id": "tc34", "text": "Тест-кейс № 34"}]

    hits = smart_search(search, "тест-кейс 34", {"project": "p"}, 10)
    assert [h["file_id"] for h in hits] == ["tc34", "x"]


def test_source_label_is_human_readable():
    email = {"doc_type": "email", "title": "Re: ТЗ на обновление", "date": "2026-09-15", "author": "Рудницкий"}
    assert source_label(email) == "письмо «Re: ТЗ на обновление», 2026-09-15, Рудницкий"
    doc = {"doc_type": "tz", "doc_title": "Техническое задание", "doc_version": "2.0", "plan_item": "64"}
    assert source_label(doc) == "Техническое задание (ред. 2.0), пункт плана № 64"


class FakeChat:
    """Модель, которая всё время вызывает один и тот же инструмент, пока её не попросят ответить."""

    def __init__(self, tool_calls_before_answer: int):
        self.left = tool_calls_before_answer
        self.requests = []

    def create(self, **kw):
        self.requests.append(kw)
        if kw.get("tool_choice") == "none" or self.left <= 0:
            msg = SimpleNamespace(tool_calls=None, content="Ответ: LTS 11.5.27")
        else:
            self.left -= 1
            call = SimpleNamespace(id=f"c{self.left}", function=SimpleNamespace(
                name="search_docs", arguments=json.dumps({"query": "11.6"})))
            msg = SimpleNamespace(tool_calls=[call], content=None,
                                  model_dump=lambda exclude_none=True: {"role": "assistant", "content": ""})
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _ctx(monkeypatch, chat):
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=chat))
    monkeypatch.setattr(agent, "client", lambda s: fake_client)
    searches = []

    def fake_search(ctx, query, filters, k):
        searches.append(query)
        return [{"источник": "письмо «Re: ТЗ»", "текст": "нужна стабильная LTS - это 11.5.27"}]

    monkeypatch.setattr(agent, "_search", fake_search)
    s = Settings(onec_bin="/nonexistent")
    return agent.ToolContext(s, "vs", Path("/nonexistent")), searches


def test_answers_immediately_from_prefetched_fragments(monkeypatch):
    chat = FakeChat(tool_calls_before_answer=0)
    ctx, searches = _ctx(monkeypatch, chat)
    r = agent.run_agent("Почему не 11.6?", ctx)
    assert r.answer == "Ответ: LTS 11.5.27" and r.steps == 1 and r.trace == []
    assert "нужна стабильная LTS" in chat.requests[0]["messages"][1]["content"]  # фрагменты уже в вопросе
    tool_names = {t["function"]["name"] for t in chat.requests[0]["tools"]}
    # нет кода, графа, платформы и ключей для интернета — остаются поиск по базе и рекомендация эксперта
    assert tool_names == {"search_docs", "prepare_escalation"}


def test_repeated_calls_deduplicated_and_final_answer_forced(monkeypatch):
    chat = FakeChat(tool_calls_before_answer=100)
    ctx, searches = _ctx(monkeypatch, chat)
    r = agent.run_agent("Почему не 11.6?", ctx, max_steps=4)
    assert r.answer == "Ответ: LTS 11.5.27"  # вместо «не удалось получить ответ»
    assert [t.get("repeat", False) for t in r.trace] == [False, True, True, True]
    assert len(searches) == 2  # предварительный поиск + один реальный вызов, повторы не выполнялись
    assert chat.requests[-1]["tool_choice"] == "none"
