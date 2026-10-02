from pathlib import Path

from copilot1c.code1c.bsl import module_owner, parse_module
from copilot1c.code1c.indexer import config_chunks
from copilot1c.code1c.metadata import parse_object_xml
from copilot1c.code1c.platform import Designer
from copilot1c.config import Settings

DUMP = Path(__file__).parent / "fixtures" / "dump"
MODULE = "Catalogs/Номенклатура/Ext/ObjectModule.bsl"


def test_parse_module_methods():
    text = (DUMP / MODULE).read_text(encoding="utf-8")
    methods = parse_module(text, MODULE)
    assert [m.name for m in methods] == ["КС_ПередЗаписью", "КС_ГлубинаВыгрузки"]

    before, depth = methods
    assert before.intercepts == [("После", "ПередЗаписью")]
    assert before.comment.startswith("Проверка совпадения")
    assert before.region == "ОбработчикиСобытий"
    assert before.text.lstrip().startswith("// Проверка")  # комментарий и аннотация входят в чанк
    assert "ОбщегоНазначения.СообщитьПользователю" in before.calls
    assert "Вызов" not in before.calls  # вызовы в комментариях не считаются

    assert depth.export and depth.kind == "Функция"
    assert depth.context == ["НаСервере"]
    assert depth.params == "Месяцев = 36"
    assert "Справочник.Номенклатура" in depth.md_refs
    assert "КС_Гамма.Отбор" in depth.calls
    assert depth.region == "СлужебныеПроцедурыИФункции"


def test_module_owner():
    assert module_owner(MODULE) == "Справочник.Номенклатура"
    assert module_owner("CommonModules/КС_Общий/Ext/Module.bsl") == "ОбщийМодуль.КС_Общий"


def test_metadata_card():
    md = parse_object_xml(DUMP / "Catalogs" / "Номенклатура.xml")
    assert md.full_name == "Справочник.Номенклатура"
    assert md.adopted
    assert md.attributes[0].name == "КС_Гамма"
    assert md.attributes[0].types == ["cfg:CatalogRef.КС_Гамма"]
    assert "КС_Аналоги" in md.tabular_sections
    assert "Табличная часть КС_Аналоги: Аналог" in md.card()


def test_config_chunks():
    chunks = config_chunks(DUMP, config="УТ 11.5.27.75", project="p")
    titles = {c.title for c in chunks}
    assert "Справочник.Номенклатура.КС_ПередЗаписью" in titles
    assert "Справочник.Номенклатура" in titles
    method = next(c for c in chunks if c.title.endswith("КС_ПередЗаписью"))
    assert method.attributes()["custom"] == "true"
    assert method.attributes()["intercept"] == "true"
    assert method.source.endswith("ObjectModule.bsl#L3")


def test_designer_commands(tmp_path):
    d = Designer(Settings(onec_bin="1cv8", sandbox_ib_path="/ib"))
    log = tmp_path / "out.log"
    assert d.load_cfg_cmd("x.cfe", log, "КС_Расширение")[-4:] == ["/LoadCfg", "x.cfe", "-Extension", "КС_Расширение"]
    assert d.dump_external_cmd("ПФ.ВыгрузкаНСИ.epf", "/out", log)[-3:] == [
        "/DumpExternalDataProcessorOrReportToFiles", "/out/ПФ.ВыгрузкаНСИ.xml", "ПФ.ВыгрузкаНСИ.epf"]
    assert d.check_modules_cmd(log)[:4] == ["1cv8", "DESIGNER", "/F", "/ib"]
