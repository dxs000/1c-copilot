"""Оценка качества: эталонные вопросы → поиск (recall@k) и ответы агента (наличие обязательных фактов).

Формат набора (JSON-массив):
  {
    "id": "upgrade-why-not-11.6",
    "question": "Почему обновляемся на 11.5.27.75, а не на 11.6?",
    "evidence": ["11.6 появилась после запуска обновления"],   # фрагмент, который должен найтись в top-k
    "answer_must_contain": ["LTS", "11.5.27"],                  # что обязано быть в ответе агента
    "filters": {"doc_type": "email"}                            # необязательно
  }
Сравнение без учёта регистра, пробелов и кавычек-ёлочек, чтобы не зависеть от форматирования.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Case:
    id: str
    question: str
    evidence: list[str] = field(default_factory=list)
    answer_must_contain: list[str] = field(default_factory=list)
    filters: dict[str, str] = field(default_factory=dict)


@dataclass
class CaseResult:
    id: str
    question: str
    retrieval_hit: bool | None = None
    hit_rank: int | None = None
    missing_evidence: list[str] = field(default_factory=list)
    answer_ok: bool | None = None
    missing_facts: list[str] = field(default_factory=list)
    answer: str = ""
    error: str = ""
    seconds: float = 0.0


def norm(text: str) -> str:
    text = text.casefold().replace("ё", "е")
    text = re.sub(r"[«»\"“”„'`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def load_cases(path: str | Path) -> list[Case]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Case(**item) for item in data]


def check_retrieval(case: Case, hits: list[dict]) -> tuple[bool, int | None, list[str]]:
    """Каждый фрагмент evidence должен встретиться хотя бы в одном из найденных чанков."""
    texts = [norm(h.get("text", "")) for h in hits]
    missing, ranks = [], []
    for ev in case.evidence:
        rank = next((i + 1 for i, t in enumerate(texts) if norm(ev) in t), None)
        if rank is None:
            missing.append(ev)
        else:
            ranks.append(rank)
    return not missing, (max(ranks) if ranks and not missing else None), missing


def check_answer(case: Case, answer: str) -> tuple[bool, list[str]]:
    a = norm(answer)
    missing = [f for f in case.answer_must_contain if norm(f) not in a]
    return not missing, missing


def run(cases: list[Case], search: Callable[[str, dict, int], list[dict]],
        answer: Callable[[str], str] | None = None, k: int = 10,
        progress: Callable[[str], None] | None = None) -> list[CaseResult]:
    results = []
    for i, case in enumerate(cases, 1):
        r = CaseResult(id=case.id, question=case.question)
        t0 = time.monotonic()
        try:
            if case.evidence:
                hits = search(case.question, case.filters, k)
                r.retrieval_hit, r.hit_rank, r.missing_evidence = check_retrieval(case, hits)
            if answer is not None and case.answer_must_contain:
                r.answer = answer(case.question)
                r.answer_ok, r.missing_facts = check_answer(case, r.answer)
        except Exception as exc:  # noqa: BLE001 — один упавший вопрос не останавливает прогон
            r.error = f"{type(exc).__name__}: {exc}"[:300]
        r.seconds = round(time.monotonic() - t0, 1)
        results.append(r)
        if progress:
            mark = "✓" if r.retrieval_hit and r.answer_ok is not False else ("!" if r.error else "✗")
            progress(f"  [{i}/{len(cases)}] {mark} {case.id}")
    return results


def summary(results: list[CaseResult], k: int) -> dict:
    ret = [r for r in results if r.retrieval_hit is not None]
    ans = [r for r in results if r.answer_ok is not None]
    return {
        "cases": len(results),
        f"recall@{k}": round(sum(r.retrieval_hit for r in ret) / len(ret), 3) if ret else None,
        "answers_ok": round(sum(r.answer_ok for r in ans) / len(ans), 3) if ans else None,
        "errors": sum(bool(r.error) for r in results),
    }


def report_text(results: list[CaseResult], k: int) -> str:
    s = summary(results, k)
    lines = [f"Вопросов: {s['cases']}   recall@{k}: {s[f'recall@{k}']}   ответы с фактами: {s['answers_ok']}   "
             f"ошибок: {s['errors']}"]
    for r in results:
        if r.error:
            lines.append(f"! {r.id}: {r.error}")
            continue
        if r.retrieval_hit is False:
            lines.append(f"✗ поиск  {r.id}: не найдено {r.missing_evidence}")
        if r.answer_ok is False:
            lines.append(f"✗ ответ  {r.id}: нет {r.missing_facts} — «{r.answer[:160]}…»")
    return "\n".join(lines)


def save(results: list[CaseResult], k: int, out_dir: str | Path) -> Path:
    out = Path(out_dir) / f"eval-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary(results, k), "results": [asdict(r) for r in results]},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    return out
