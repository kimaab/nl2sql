import describe
from compiler import Dialect
from describe import build_prompt, describe_table, infer_relations, read_answer
from test_ask import FakeModel


def table(name, *columns):
    cols = []
    for c in columns:
        name_, type_, pk = (c + (False,))[:3]
        cols.append({"name": name_, "type": type_, "is_pk": pk})
    return {"name": name, "columns": cols}


def pairs(relations):
    out = []
    for r in relations:
        out.append((f"{r['left_table']}.{r['left_column']}", f"{r['right_table']}.{r['right_column']}"))
    return sorted(out)


def test_same_named_key_points_to_its_table():
    tables = [
        table("vehicle", ("vehicle_id", "varchar(20)", True), ("vehicle_name", "varchar")),
        table("bms_log", ("log_id", "bigint", True), ("vehicle_id", "varchar(20)")),
    ]
    assert pairs(infer_relations(tables)) == [("bms_log.vehicle_id", "vehicle.vehicle_id")]


def test_generic_id_needs_table_name_in_column():
    tables = [
        table("tb_vehicle", ("id", "bigint", True)),
        table("tb_driver", ("id", "bigint", True)),
        table("trip", ("id", "bigint", True), ("vehicle_id", "integer"), ("driver_id", "bigint"), ("code_id", "int")),
    ]
    assert pairs(infer_relations(tables)) == [("trip.driver_id", "tb_driver.id"), ("trip.vehicle_id", "tb_vehicle.id")]


def test_extension_table_points_to_home_table_once():
    tables = [
        table("vehicle", ("vehicle_id", "varchar", True)),
        table("vehicle_ext", ("vehicle_id", "varchar", True), ("color", "varchar")),
    ]
    assert pairs(infer_relations(tables)) == [("vehicle_ext.vehicle_id", "vehicle.vehicle_id")]


def test_type_family_must_match_and_ambiguous_keys_are_skipped():
    tables = [
        table("vehicle", ("vehicle_id", "varchar", True)),
        table("bms_log", ("vehicle_id", "bigint")),  # 타입이 다르다
        table("a", ("ref_key", "int", True)),
        table("b", ("ref_key", "int", True)),
        table("c", ("ref_key", "int")),  # a 인지 b 인지 모른다
    ]
    assert infer_relations(tables) == []


def test_other_schema_table_name_matches_without_schema():
    tables = [
        table("sales.vehicle", ("id", "int", True)),
        table("trip", ("vehicle_id", "int")),
    ]
    assert pairs(infer_relations(tables)) == [("trip.vehicle_id", "sales.vehicle.id")]


def test_read_answer_keeps_only_asked():
    answer = {"table": " 차량  운행 기록 ", "columns": {"VEHICLE_ID": "차량 번호", "speed": "속도", "x": ""}}
    assert read_answer(answer, True, ["vehicle_id", "x"]) == ("차량 운행 기록", {"vehicle_id": "차량 번호"})
    assert read_answer(answer, False, []) == ("", {})


TARGET = {
    "id": "t1",
    "name": "bms_log",
    "schema": "",
    "comment": "",
    "need_table": True,
    "columns": [
        {"name": "vehicle_id", "type": "varchar", "comment": "차량 ID", "is_pk": False, "need": False},
        {"name": "st_cd", "type": "char(1)", "comment": "", "is_pk": False, "need": True},
    ],
}


def test_prompt_marks_what_to_fill_and_shows_samples():
    text = build_prompt({"domain_desc": "차량 BMS"}, TARGET, ["st_cd"], [["V1", "R"], ["V2", "S"]], "")
    assert "업무 영역: 차량 BMS" in text
    assert "- vehicle_id | varchar | 차량 ID" in text
    assert "- st_cd | char(1) | (설명 필요)" in text
    assert '["V1", "R"]' in text
    assert text.endswith("설명이 필요한 컬럼: st_cd")


def test_describe_table_stores_answer(monkeypatch):
    stored = []
    monkeypatch.setattr(describe, "_samples", lambda row, d, t: ([["V1", "R"]], ""))
    monkeypatch.setattr(describe, "_store", lambda tid, td, cd: stored.append((tid, td, cd)))
    model = FakeModel({"table": "BMS 수집 로그", "columns": {"st_cd": "운행 상태 코드 (R=운행)", "vehicle_id": "무시"}})
    assert describe_table({"driver": "postgresql"}, Dialect("postgresql"), TARGET, model) == (True, 1)
    assert stored == [("t1", "BMS 수집 로그", {"st_cd": "운행 상태 코드 (R=운행)"})]
