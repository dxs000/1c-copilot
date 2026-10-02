from types import SimpleNamespace

from typer.testing import CliRunner

from copilot1c.config import Settings
from copilot1c.index import yandex
from copilot1c.models import Chunk, DocType


class FakeClient:
    def __init__(self):
        self.uploaded: list[str] = []
        self.stored: dict[str, dict] = {}
        outer = self

        class Files:
            def create(self, file, purpose):
                outer.uploaded.append(file[0])
                return SimpleNamespace(id=f"file-{len(outer.uploaded)}")

            def retrieve(self, file_id):
                return SimpleNamespace(filename=outer.stored[file_id]["name"])

        class VSFiles:
            def create(self, vector_store_id, file_id, attributes):
                outer.stored[file_id] = {"name": outer.uploaded[-1], "attrs": attributes}

            def list(self, vector_store_id, limit):
                return [SimpleNamespace(id=k, attributes={}) for k in outer.stored]

        self.files = Files()
        self.vector_stores = SimpleNamespace(files=VSFiles())


def _chunks(n):
    return [Chunk(text=f"текст {i}", doc_type=DocType.EMAIL, source=f"s{i}") for i in range(n)]


def test_add_is_resumable(monkeypatch, tmp_path):
    fake = FakeClient()
    monkeypatch.setattr(yandex, "client", lambda s: fake)
    s = Settings(cache_dir=str(tmp_path))
    index = yandex.VectorIndex("vs1", s)

    ids = index.add(_chunks(3))
    assert len(fake.uploaded) == 3 and len(ids) == 3
    assert all("chunk_id" in v["attrs"] for v in fake.stored.values())

    ids = index.add(_chunks(5))  # повторный запуск с новыми чанками: загружаются только новые
    assert len(fake.uploaded) == 5 and len(ids) == 5


def test_rebuild_manifest_after_crash(monkeypatch, tmp_path):
    fake = FakeClient()
    monkeypatch.setattr(yandex, "client", lambda s: fake)
    index = yandex.VectorIndex("vs1", Settings(cache_dir=str(tmp_path)))
    index.add(_chunks(4))
    index.manifest_path.unlink()  # прошлый запуск упал, манифеста нет

    restored = index.rebuild_manifest(progress=lambda m: None)
    assert set(restored) == {c.chunk_id for c in _chunks(4)}
    index.add(_chunks(4))
    assert len(fake.uploaded) == 4  # дублей нет


def test_index_docs_without_postgres(monkeypatch, tmp_path):
    from copilot1c import cli
    from copilot1c.graph import store

    fake = FakeClient()
    monkeypatch.setattr(yandex, "client", lambda s: fake)
    monkeypatch.setattr(store, "try_connect", lambda s=None: None)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(cache_dir=str(tmp_path), vector_store_id="vs1"))
    (tmp_path / "note.txt").write_text("ДС № 10 на обновление УТ 11.5.27.75", encoding="utf-8")

    result = CliRunner().invoke(cli.app, ["index-docs", str(tmp_path / "note.txt")])
    assert result.exit_code == 0, result.output
    assert "PostgreSQL недоступен" in result.output
    assert len(fake.uploaded) == 1
