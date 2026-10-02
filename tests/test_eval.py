from pathlib import Path

from copilot1c import eval as ev

EXAMPLE = Path(__file__).parent / "eval" / "example.json"


def test_retrieval_and_answer_checks():
    cases = ev.load_cases(EXAMPLE)
    hits = {
        cases[0].question: [{"text": "шум"}, {"text": "Так как минимальная версия  платформы 8.3.27.1859, подходит"}],
        cases[1].question: [{"text": "ничего"}],
    }
    seen_filters = []

    def search(q, filters, k):
        seen_filters.append(filters)
        return hits[q][:k]

    answers = {cases[0].question: "Да, минимум 8.3.27.1859", cases[1].question: "Не знаю"}
    results = ev.run(cases, search, answers.get, k=10)

    ok, miss = results
    assert ok.retrieval_hit and ok.hit_rank == 2 and ok.answer_ok
    assert miss.retrieval_hit is False and miss.missing_evidence == ["custsrvapp01"]
    assert miss.answer_ok is False and miss.missing_facts == ["custsrvapp01"]
    assert seen_filters[1] == {"doc_type": "email"}
    assert ev.summary(results, 10) == {"cases": 2, "recall@10": 0.5, "answers_ok": 0.5, "errors": 0}
    assert "✗ поиск  test-server" in ev.report_text(results, 10)


def test_errors_do_not_stop_run():
    cases = ev.load_cases(EXAMPLE)

    def search(q, filters, k):
        raise RuntimeError("vector store недоступен")

    results = ev.run(cases, search, None)
    assert all(r.error.startswith("RuntimeError") for r in results)
    assert ev.summary(results, 10)["errors"] == 2


def test_norm_ignores_quotes_case_and_spaces():
    assert ev.norm("ПиМИ «01 НСИ»") in ev.norm('Документ пими "01  нси" покрывает')
