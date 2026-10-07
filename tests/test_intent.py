"""Тип сообщения в чате: эвристики на размеченном наборе, черновик обращения, уточнение моделью, API."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from copilot1c import intent as it
from copilot1c import server
from copilot1c.config import Settings

ERROR = "{Документ.РеализацияТоваровУслуг.МодульОбъекта(245)}: Поле объекта не обнаружено (КС_Гамма)"

# (сообщение, главный тип, предлагать ли регистрацию обращения)
CASES = [
    ("Почему обновляемся на 11.5.27.75, а не на 11.6?", "question", False),
    ("Подходит ли платформа 8.3.27.2342 для УТ 11.5.27.75?", "question", False),
    ("Как ПФ.ВыгрузкаНСИ.epf отбирает НСИ по глубине 36 месяцев?", "question", False),
    ("Что вычеркнули из ДС № 10?", "question", False),
    ("Какие требования ТЗ к справочнику Номенклатура не покрыты ПиМИ?", "summary", False),
    ("Список всех доработок КС, которые затронет обновление", "summary", False),
    ("Сколько тест-кейсов ПиМИ в статусе «Не работает»?", "summary", False),
    ("Подготовь письмо заказчику о переносе срока испытаний на 20.10", "document", False),
    ("Напиши резюме переписки по обновлению за последнюю неделю", "document", False),
    ("К сведению: договорились, что переход на 11.6 только через 11.5.27", "knowledge", False),
    ("Платформа 8.3.27.1786 устарела, теперь версия 8.3.27.2342 на всех серверах", "knowledge", False),
    (f"После обновления не проводится реализация у отдела продаж, ошибка:\n{ERROR}", "issue", True),
    ("Пользователи сообщают, что не формируется отчёт по остаткам, пустой отчет", "issue", True),
    ("Бланк заказа не грузится в 1С, ошибка при загрузке", "issue", True),
    ("Ошибка при вызове метода контекста (Записать) при записи номенклатуры", "issue", True),
    ("Срочно! Склад не может отгружать: не проводится расходный ордер", "issue", True),
    ("После обновления очень медленно проводится реализация, по 2 минуты", "issue", True),
    ("Не проводится реализация у менеджеров", "issue", True),
    ("Не работает выгрузка НСИ?", "question", False),  # короткий вопрос без деталей — решает модель
    ("У бухгалтерии не сходятся остатки по счёту 41 после выгрузки НСИ", "issue", True),
    # вопрос о сбое: главный — проблема или вопрос, но обращение предлагается
    (f"Почему не проводится реализация? {ERROR}", "issue", True),
]


@pytest.mark.parametrize(("text", "primary", "issue"), CASES, ids=[c[0][:40] for c in CASES])
def test_heuristics_on_labeled_messages(text, primary, issue):
    r = it.heuristics(text)
    assert r.primary == primary, (r.primary, r.scores, r.signals)
    assert r.is_issue is issue, (r.scores, r.signals)


def test_signals_explain_decision_and_files_count():
    r = it.heuristics("Не открывается форма заказа", has_files=True)
    assert r.is_issue and any("приложены файлы" in s for s in r.signals)
    d = r.to_dict()
    assert d["primary"] == "issue" and d["primary_label"] == "проблема" and d["method"] == "heuristic"
    assert it.heuristics("привет").primary == "question"  # ничего не сработало — вопрос к базе


def test_issue_draft_fields():
    text = (f"Добрый день!\nПосле обновления не проводится реализация у всех менеджеров.\n{ERROR}\n"
            "Справочник «Номенклатура» тоже не записывается.")
    d = it.issue_draft(text)
    assert d["title"] == "После обновления не проводится реализация у всех менеджеров"
    assert d["error_text"] == ERROR and d["description"].startswith("Добрый день!")
    assert d["objects"][:2] == ["Документ.РеализацияТоваровУслуг", "КС_Гамма"]
    assert "МодульОбъекта" not in d["objects"] and d["category"] == "bug" and d["priority"] == "high"
    assert d["source"] == "chat"
    assert it.issue_draft("Очень медленно проводится реализация")["category"] == "performance"
    assert it.issue_draft("Нарушение прав доступа при открытии отчета")["category"] == "access"
    assert it.issue_draft("Склад не может отгружать, встала отгрузка")["priority"] == "critical"
    long_title = it.issue_draft("Не работает " + "очень " * 40 + "долго")["title"]
    assert len(long_title) <= 120 and long_title.endswith("…")


def _settings(**kw):
    return SimpleNamespace(intent_llm=True, yc_api_key="k", yc_folder_id="f", model_batch="m", **kw)


def test_llm_only_when_unsure(monkeypatch):
    from copilot1c.index import yandex

    calls = []
    monkeypatch.setattr(yandex, "chat_json", lambda *a, **kw: calls.append(a) or {"intents": ["issue", "question"],
                                                                                 "reason": "жалоба пользователя"})
    sure = it.classify(f"Ошибка: {ERROR}", settings=_settings())
    assert sure.method == "heuristic" and not calls  # уверенный случай — без модели
    unsure = it.classify("Бланк заказа в 1С", settings=_settings())
    assert unsure.method == "llm" and unsure.primary == "issue" and unsure.is_issue and len(calls) == 1
    assert any("модель: проблема, вопрос" in s for s in unsure.signals)
    assert it.classify("Бланк заказа в 1С", settings=_settings(), use_llm=False).method == "heuristic"
    no_keys = SimpleNamespace(intent_llm=True, yc_api_key="", yc_folder_id="", model_batch="m")
    assert it.classify("Бланк заказа в 1С", settings=no_keys).method == "heuristic"


def test_llm_says_not_an_issue_and_llm_failure(monkeypatch):
    from copilot1c.index import yandex

    text = "Почему не работает выгрузка, ошибка?"
    h = it.heuristics(text)
    assert h.is_issue and not h.confident  # вопрос и проблема почти равны — решает модель
    monkeypatch.setattr(yandex, "chat_json", lambda *a, **kw: {"intents": ["question"]})
    r = it.classify(text, settings=_settings())
    assert r.primary == "question" and not r.is_issue

    def boom(*a, **kw):
        raise TimeoutError("AI Studio")

    monkeypatch.setattr(yandex, "chat_json", boom)
    r = it.classify(text, settings=_settings())
    assert r.method == "heuristic" and any("модель недоступна" in s for s in r.signals)


def test_classify_endpoint_and_ask_payload(monkeypatch):
    s = Settings(yc_api_key="", yc_folder_id="", onec_bin="/nonexistent")
    c = TestClient(server.create_app(s))
    r = c.post("/classify", json={"text": f"После обновления не проводится реализация.\n{ERROR}"}).json()
    assert r["intent"]["primary"] == "issue" and r["issue_draft"]["error_text"] == ERROR
    q = c.post("/classify", json={"text": "Почему обновляемся на 11.5.27.75?"}).json()
    assert q["intent"]["primary"] == "question" and q["issue_draft"] is None

    def broken(*a, **kw):
        raise RuntimeError("сломалось")

    monkeypatch.setattr(it, "classify", broken)
    out = server.classify_message(s, "что угодно")
    assert out["issue_draft"] is None and "сломалось" in out["intent"]["error"]
