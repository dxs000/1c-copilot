"""Команды платформы 1С для ВМ-песочницы: распаковка .cf/.cfe/.epf в XML/BSL и сборка расширения.

Платформа запускается только в изолированной файловой базе-песочнице; рабочие базы недоступны.
Каждая функция *_cmd возвращает argv, run() выполняет его и возвращает журнал /Out.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from copilot1c.config import Settings, get_settings


@dataclass
class RunResult:
    argv: list[str]
    returncode: int
    log: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Designer:
    def __init__(self, settings: Settings | None = None, ib_path: str | None = None):
        self.s = settings or get_settings()
        self.ib = ib_path or self.s.sandbox_ib_path

    # --- базовые аргументы ---
    def _base(self, log: Path) -> list[str]:
        return [self.s.onec_bin, "DESIGNER", "/F", self.ib, "/DisableStartupDialogs",
                "/DisableStartupMessages", "/Out", str(log)]

    @staticmethod
    def _ext(extension: str | None) -> list[str]:
        return ["-Extension", extension] if extension else []

    # --- команды ---
    def create_ib_cmd(self) -> list[str]:
        return [self.s.onec_bin, "CREATEINFOBASE", f'File="{self.ib}"']

    def load_cfg_cmd(self, file: str | Path, log: Path, extension: str | None = None) -> list[str]:
        return self._base(log) + ["/LoadCfg", str(file), *self._ext(extension)]

    def dump_files_cmd(self, out_dir: str | Path, log: Path, extension: str | None = None) -> list[str]:
        return self._base(log) + ["/DumpConfigToFiles", str(out_dir), *self._ext(extension)]

    def dump_external_cmd(self, file: str | Path, out_dir: str | Path, log: Path) -> list[str]:
        # Выгрузка внешней обработки/отчёта: /DumpExternalDataProcessorOrReportToFiles <xml-root> <epf>
        root = Path(out_dir) / (Path(file).stem + ".xml")
        return self._base(log) + ["/DumpExternalDataProcessorOrReportToFiles", str(root), str(file)]

    def load_files_cmd(self, src_dir: str | Path, log: Path, extension: str | None = None) -> list[str]:
        return self._base(log) + ["/LoadConfigFromFiles", str(src_dir), *self._ext(extension)]

    def update_db_cmd(self, log: Path, extension: str | None = None) -> list[str]:
        return self._base(log) + ["/UpdateDBCfg", *self._ext(extension)]

    def check_modules_cmd(self, log: Path, extension: str | None = None) -> list[str]:
        modes = ["-ThinClient", "-Server", "-ExternalConnection"]
        return self._base(log) + ["/CheckModules", *modes, *self._ext(extension)]

    def dump_cfg_cmd(self, file: str | Path, log: Path, extension: str | None = None) -> list[str]:
        return self._base(log) + ["/DumpCfg", str(file), *self._ext(extension)]

    # --- выполнение ---
    def run(self, build) -> RunResult:  # build: Callable[[Path], list[str]]
        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as tmp:
            log = Path(tmp.name)
        argv = build(log)
        proc = subprocess.run(argv, capture_output=True, timeout=3600)
        text = log.read_text(encoding="utf-8-sig", errors="replace") if log.exists() else ""
        log.unlink(missing_ok=True)
        return RunResult(argv=argv, returncode=proc.returncode, log=text)

    # --- сценарии конвейера ---
    def unpack(self, file: str | Path, out_dir: str | Path, extension: str | None = None) -> list[RunResult]:
        """.cf/.cfe → XML/BSL; .epf/.erf → XML/BSL. Останавливается на первой ошибке."""
        suffix = Path(file).suffix.lower()
        if suffix in {".epf", ".erf"}:
            return [self.run(lambda log: self.dump_external_cmd(file, out_dir, log))]
        steps = [
            lambda log: self.load_cfg_cmd(file, log, extension),
            lambda log: self.dump_files_cmd(out_dir, log, extension),
        ]
        return self._chain(steps)

    def build_extension(self, src_dir: str | Path, out_cfe: str | Path, extension: str) -> list[RunResult]:
        """Сборка и проверка расширения: загрузка из файлов → обновление БД → /CheckModules → .cfe."""
        steps = [
            lambda log: self.load_files_cmd(src_dir, log, extension),
            lambda log: self.update_db_cmd(log, extension),
            lambda log: self.check_modules_cmd(log, extension),
            lambda log: self.dump_cfg_cmd(out_cfe, log, extension),
        ]
        return self._chain(steps)

    def _chain(self, steps) -> list[RunResult]:
        results: list[RunResult] = []
        for step in steps:
            r = self.run(step)
            results.append(r)
            if not r.ok:
                break
        return results
