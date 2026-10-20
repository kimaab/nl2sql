"""지표 초안: 지표가 없어 멈춘 질문에서 LLM 이 지표 SQL 초안을 쓴다. 저장하지 않는다 — 사람이 확인한 뒤 등록한다.

재료는 대상 테이블의 컬럼(계산 필요 · UTC 표시), 인덱스, 같은 테이블에 이미 있는 지표, 대상 컬럼의 실제 값 샘플이다.
값 샘플은 인덱스를 타는 조회로만 뽑는다 — 인덱스 앞 칸에 값이나 범위를 걸고, 인덱스 칸 순서로 몇 행만 읽는다.
지표가 필요한 테이블은 대개 로그처럼 큰 테이블이라, 인덱스 없이 훑으면 데이터가 적어도 운영 DB 에 부담이 된다.
초안 SQL 은 등록과 같은 확인(AST 변환 → 컴파일 → 결과 비교)을 거치고, 실패하면 오류를 붙여 다시 쓰게 한다.
"""

import datetime
import json
import logging
from uuid import UUID

import sqlglot
from langchain_core.messages import HumanMessage, SystemMessage
from sqlglot import exp

from catalog import read_indexes
from compiler import Dialect, temporal_kind
from contract import load_contract
from metrics import STAGE_LABEL, check_metric, read_only
from models import ApiException, MetricDraft, MetricDraftRequest, ValueSample
from selector import Step, chat, model_name, parse_json
from sql_ast import DIALECTS

log = logging.getLogger("nl2sql.draft")

MAX_ATTEMPTS = 3
SAMPLE_TIMEOUT_S = 10  # 샘플 조회 하나의 제한. 인덱스를 타면 금방 끝난다
SAMPLE_DAYS = 3  # 날짜별 첫 값 · 마지막 값을 볼 날 수 (누적값이 날을 넘어 이어지는지, 리셋되는지)
RECENT_ROWS = 10  # 가장 최근 몇 행 (기록 간격과 값이 커지는 모양)
CANDIDATE_ROWS = 50  # 묶음(차량) 후보를 고를 때 읽는 최근 행 수
CANDIDATES = 5  # 그중 값이 바뀌는 묶음을 찾아볼 개수
SCAN_ROWS = 300  # 묶음(장비) 인덱스가 없을 때 최근 행을 이만큼 읽어 묶음 하나를 고른다
SHOW_ROWS = 30
DRAFT_DAYS = 3  # 초안 SQL 의 예시 기간. 등록 확인이 대상 DB 에서 원본과 옮긴 SQL 을 모두 돌리므로 짧게
SMALL_TABLE_ROWS = 10_000  # 이보다 작은 테이블은 전체를 훑어도 된다 (작으면 실행 계획이 일부러 그렇게 고른다)

DRAFT_SYSTEM = """당신은 '{name}' 데이터베이스({driver})에 등록할 지표의 SQL 초안을 쓰는 역할입니다.
지표는 사람이 확인한 뒤 등록하고, 이후 질문마다 다른 LLM 이 이 SQL 의 기간·조건·묶음만 바꿔 씁니다.
{domain}
[규칙]
- [같은 테이블 지표] 가 있으면 그 SQL 의 묶음 단위(예: 일 × 장비)와 시각 처리 방식을 따릅니다.
- 질문의 기간·개수·정렬은 지표에 넣지 않습니다. 지표는 계산의 기본 단위입니다 (예: '최근 7일 합계' 를 물어도 지표는 일별 값).
- [계산 필요] 컬럼은 그대로 SUM·AVG 하지 않습니다. 컬럼 설명과 [값 샘플] 로 계산 방식을 정합니다
  (예: 날을 넘어 계속 커지는 누적값이면 하루의 MAX - MIN).
- 기간 조건은 예시 값으로 {anchor} 까지 {days}일을 적습니다.
- 기간 조건은 [인덱스] 에 있는 시각 컬럼에 걸고, WHERE 에서 그 컬럼을 함수나 계산으로 감싸지 않습니다 (인덱스를 타게).
- [UTC +n시간] 컬럼은 UTC 로 저장된 시각입니다. 현지 날짜로 묶을 때는 SELECT·GROUP BY 에서 '컬럼 + n시간' 으로 적고,
  WHERE 의 기간 조건은 컬럼을 그대로 두고 UTC 값으로 적습니다.
- GROUP BY · ORDER BY 에 순번(1, 2)을 쓰지 않습니다. SELECT 의 식이나 컬럼을 그대로 적습니다.
- SELECT 문 하나만 씁니다. 윈도 함수는 ROW_NUMBER 만, DISTINCT ON · OFFSET · 셀프 조인 · 하위 쿼리 JOIN 은 쓰지 않습니다.
- name 은 [빠진 계산] 의 이름을 그대로 쓰는 것을 기본으로 합니다.
- description 에는 무엇을 어떻게 계산하는지 적습니다. 질문을 받은 LLM 이 이 지표가 맞는지 판단할 때 읽습니다.
- assumptions 에는 사람이 확인해야 할 가정을 적습니다 (예: 하루 중 누적값이 리셋되지 않는다).
- 계산 방식을 정할 수 없으면 sql 을 빈 문자열로 두고 unsure 에 이유를 적습니다.
- 설명 없이 JSON 객체 하나로만 답합니다:
  {{"sql": "SELECT ...", "name": "지표 이름", "synonyms": ["같은 말"], "description": "계산 설명",
    "assumptions": ["확인할 가정"], "unsure": ""}}"""


def draft_metric(system_id: UUID, req: MetricDraftRequest, model, row: dict) -> MetricDraft:
    """초안 하나를 쓴다. row 는 대상 DB 접속 정보 (systems.connection_row)."""
    contract = load_contract(system_id, active_only=True)
    table = None
    for t in contract["tables"]:
        if t["name"] == req.missing.table:
            table = t
            break
    if table is None:
        raise ApiException(400, f"메타데이터에 없는 테이블입니다 -> {req.missing.table}")

    indexes, sample = collect(row, table, req.missing.column)
    system, user = draft_prompt(contract, table, req, indexes, sample)
    messages = [SystemMessage(system), HumanMessage(user)]
    step = Step("draft", model=model_name(model))
    draft = MetricDraft(sample=sample)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        reply = chat(model, messages, step, attempt)
        draft.attempts = attempt
        try:
            parsed = parse_json(reply.content)
        except ValueError as error:
            messages += [reply, HumanMessage(f"형식이 틀렸습니다: {error}. 설명 없이 JSON 객체 하나만 다시 답하십시오.")]
            continue
        _fill(draft, parsed)
        if draft.unsure and not draft.sql:
            break
        if not draft.sql:
            messages += [reply, HumanMessage("sql 이 비었습니다. SQL 을 적거나, 정할 수 없으면 unsure 에 이유를 적으십시오.")]
            continue
        # 대상 DB 에서 실행하기 전에 순번 참조와 실행 계획을 본다 — 걸리면 실행하지 않고 고쳐 쓰게 한다
        draft.full_scans = []
        draft.check = check_metric(system_id, draft.sql, guard=_guard(row, draft))
        if not draft.check.error:
            break
        stage = STAGE_LABEL.get(draft.check.stage, "확인 실패")
        messages += [reply, HumanMessage(f"{stage}: {draft.check.error}\n고쳐서 JSON 으로 다시 답하십시오.")]
    log.info(
        "지표 초안 %s.%s: attempts=%d ok=%s unsure=%s",
        table["name"], req.missing.column, draft.attempts,
        bool(draft.check and not draft.check.error), draft.unsure,
    )
    return draft


def _guard(row: dict, draft: MetricDraft):
    """등록 확인이 대상 DB 에서 실행하기 직전에 부르는 검사. 실행하면 안 되는 이유를 돌려준다."""

    def guard(original: str, compiled: str) -> str | None:
        positional = positional_refs(original, row["driver"])
        if positional:
            return f"{', '.join(positional)} 에 순번(1, 2 …)을 썼습니다 — 순번 대신 컬럼이나 식을 그대로 적으십시오"
        # 원본은 등록 확인에서, 옮긴 SQL 은 질문마다 실행된다 — 둘 다 본다
        draft.full_scans = _plan_full_scans(row, original, compiled)
        if draft.full_scans:
            return (
                f"실행 계획이 {', '.join(draft.full_scans)} 전체를 훑어 실행하지 않았습니다 — "
                "기간 조건을 인덱스가 있는 시각 컬럼에 함수나 계산 없이 거십시오"
            )
        return None

    return guard


def positional_refs(sql: str, driver: str) -> list[str]:
    """GROUP BY · ORDER BY 의 순번 참조 (GROUP BY 1, 2). 지표 SQL 은 질문마다 LLM 이 읽는다 — 순번은 SELECT 목록과
    맞춰 봐야 해서 헷갈리고, 조건을 바꾸다 SELECT 순서가 바뀌면 뜻이 달라진다. 읽지 못하면 빈 목록 (문법 오류는 AST 변환이 알린다)."""
    try:
        trees = sqlglot.parse(sql, read=DIALECTS.get(driver, "postgres"))
    except sqlglot.errors.ParseError:
        return []
    found = []
    for tree in trees:
        if tree is None:
            continue
        for node in tree.find_all(exp.Group, exp.Order):
            clause = "GROUP BY" if isinstance(node, exp.Group) else "ORDER BY"
            for e in node.expressions:
                target = e.this if isinstance(e, exp.Ordered) else e
                if isinstance(target, exp.Literal) and not target.is_string and clause not in found:
                    found.append(clause)
    return found


def _plan_full_scans(row: dict, *sqls: str) -> list[str]:
    """SQL 들의 실행 계획(EXPLAIN 만, 실행하지 않는다)에서 전체를 훑는 큰 테이블. 못 보면 빈 목록이다."""
    found = []
    try:
        with read_only(row, SAMPLE_TIMEOUT_S) as cur:
            for sql in sqls:
                for name in full_scans(cur, Dialect(row["driver"]), sql):
                    if name not in found:
                        found.append(name)
    except Exception as error:
        log.warning("초안 SQL 의 실행 계획을 보지 못했습니다: %s", str(error).strip().splitlines()[:1])
    return found


def _fill(draft: MetricDraft, parsed: dict) -> None:
    draft.sql = str(parsed.get("sql") or "").strip().rstrip(";").strip()
    draft.name = str(parsed.get("name") or "").strip()
    draft.description = " ".join(str(parsed.get("description") or "").split())
    draft.unsure = str(parsed.get("unsure") or "").strip() or None
    draft.synonyms = _texts(parsed.get("synonyms"))
    draft.assumptions = _texts(parsed.get("assumptions"))


def _texts(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    out = []
    for v in value or []:
        text = str(v or "").strip()
        if text and text not in out:
            out.append(text)
    return out


# ── 프롬프트 ───────────────────────────────────────────────────────────────


def draft_prompt(contract: dict, table: dict, req: MetricDraftRequest, indexes: list, sample: ValueSample) -> tuple:
    """→ (시스템 프롬프트, 사용자 메시지)"""
    driver = contract.get("driver") or "postgresql"
    anchor = sample.latest[:10] if sample.latest else f"{datetime.date.today():%Y-%m-%d}"
    domain = f"[업무 영역]\n{contract['domain']}\n" if contract.get("domain") else ""
    system = DRAFT_SYSTEM.format(
        name=contract.get("name", ""), driver=driver, domain=domain, anchor=anchor, days=DRAFT_DAYS
    )

    note = table.get("purpose") or table.get("description") or ""
    lines = [f"[테이블]\n[{table['name']}] {' '.join(note.split())}"]
    for c in table["columns"]:
        lines.append(_column_line(c, driver))

    lines.append("\n[인덱스]")
    for ix in indexes:
        lines.append(f"  - {ix['name']} ({', '.join(ix['columns'])})")
    if not indexes:
        lines.append("  (없음)")

    same, others = [], []
    for m in contract.get("metrics") or []:
        (same if table["name"] in (m.get("tables") or []) else others).append(m)
    examples = same or others[:2]
    if examples:
        lines.append("\n[같은 테이블 지표]" if same else "\n[다른 지표 예시] (같은 테이블 지표는 없습니다)")
        for m in examples:
            desc = " ".join(str(m.get("description") or "").split()) or "(설명 없음)"
            lines.append(f"- {m['name']}: {desc}\n  SQL:\n{m['sql']}")

    lines.append(f"\n[값 샘플] {sample.note}")
    if sample.rows:
        lines.append(" | ".join(sample.columns))
        for r in sample.rows:
            lines.append(" | ".join(r))
    if sample.latest:
        lines.append(f"가장 최근 시각: {sample.latest}")

    target = f"{req.missing.table}.{req.missing.column}" if req.missing.column else req.missing.table
    lines.append(f"\n[빠진 계산]\n{req.missing.name} ({target})")
    lines.append(f"\n[질문]\n{req.question}")
    return system, "\n".join(lines)


def _column_line(c: dict, driver: str) -> str:
    note = " ".join(str(c.get("description") or "").split())
    marks = []
    if c.get("utc_offset") and temporal_kind(c.get("type", ""), driver) == "datetime":
        marks.append(f"[UTC {c['utc_offset']:+d}시간]")
    if c.get("needs_calc"):
        marks.append("[계산 필요]")
    tail = " ".join([note] + marks).strip()
    return f"  - {c['name']} ({c.get('type', '')}){': ' + tail if tail else ''}"


# ── 값 샘플 (인덱스를 타는 조회만) ─────────────────────────────────────────


def collect(row: dict, table: dict, column: str) -> tuple[list, ValueSample]:
    """→ (인덱스 목록, 값 샘플). 대상 DB 를 읽지 못해도 초안은 쓴다 — 샘플이 빈 채로 note 에 이유를 남긴다."""
    names, kinds = set(), {}
    for c in table["columns"]:
        names.add(c["name"])
        kinds[c["name"]] = temporal_kind(c.get("type", ""), row["driver"])
    try:
        with read_only(row, SAMPLE_TIMEOUT_S) as cur:
            indexes = read_indexes(cur, row, table["name"], names)
            if column not in names:
                return indexes, ValueSample(note="계산할 컬럼을 몰라 값 샘플을 건너뛰었습니다")
            sampler = _Sampler(cur, Dialect(row["driver"]), table, kinds, column)
            try:
                return indexes, sampler.run(indexes)
            except _FullScan as error:
                return indexes, ValueSample(queries=sampler.queries, note=str(error))
    except ApiException as error:
        return [], ValueSample(note=f"대상 DB 를 읽지 못해 값 샘플을 건너뛰었습니다: {error.detail}")
    except Exception as error:
        lines = str(error).strip().splitlines()
        reason = lines[0] if lines else type(error).__name__
        return [], ValueSample(note=f"값 샘플 조회가 실패해 건너뛰었습니다: {reason}")


class _FullScan(Exception):
    """실행 계획이 큰 테이블을 전체 훑기로 읽는다 — 그 조회는 실행하지 않는다"""


class _Sampler:
    """대상 컬럼의 값을 인덱스 순서로 몇 행만 읽는다. 실행한 조회는 queries 에 남는다."""

    def __init__(self, cur, dialect: Dialect, table: dict, kinds: dict, column: str):
        self.cur = cur
        self.d = dialect
        self.table = table
        self.kinds = kinds
        self.column = column
        self.queries: list[str] = []
        self.offset = {}
        for c in table["columns"]:
            self.offset[c["name"]] = c.get("utc_offset") or 0

    def run(self, indexes: list) -> ValueSample:
        # 1) 묶음과 시각이 한 인덱스에 — 묶음 하나의 값을 시각 순으로 본다 (예: 차량 하나의 날짜별 첫 값 · 마지막 값)
        #    (묶음, 시각) 이면 묶음 = 값 · 시각 범위로, (시각, 묶음) 이면 시각 범위 안에서 묶음을 인덱스 칸으로 거른다
        for ix in indexes:
            cols = ix["columns"]
            if len(cols) >= 2 and self.column not in cols[:2]:
                if not self.kinds[cols[0]] and self.kinds[cols[1]]:
                    return self.series(ix, cols[0], cols[1], indexes)
                if self.kinds[cols[0]] and not self.kinds[cols[1]]:
                    return self.series(ix, cols[1], cols[0], indexes)
        # 2) 시각 인덱스와 묶음 인덱스가 따로 — 시각 범위로 읽고 묶음으로 거른다
        time_ix, group = None, None
        for ix in indexes:
            head = ix["columns"][0]
            if self.kinds[head] and time_ix is None:
                time_ix = ix
            elif not self.kinds[head] and head != self.column and group is None:
                group = head
        if time_ix is not None and group is not None:
            return self.series(time_ix, group, time_ix["columns"][0], indexes)
        # 3) 시각이 앞 칸인 인덱스 — 최근 행
        if time_ix is not None:
            return self.recent(time_ix, time_ix["columns"][0], indexes)
        # 4) 그 밖의 인덱스 — 앞 칸 큰 값부터 (일련번호 키면 최근 행이다)
        if indexes:
            return self.recent(indexes[0], indexes[0]["columns"][0], indexes)
        return ValueSample(note=f"{self.table['name']} 에 B-tree 인덱스가 없어 값 샘플을 건너뛰었습니다 (전체를 훑게 됩니다)")

    def series(self, ix: dict, group: str, time: str, indexes: list) -> ValueSample:
        q, t = self.d.q, self.d.q(self.table["name"])
        by_time = False
        for other in indexes:
            if other["columns"][0] == time:
                by_time = True
        if by_time:
            # 최근에 기록된 묶음 몇 개 — 시각 인덱스를 거꾸로 몇 행
            picked = self.fetch(
                f"SELECT {q(group)} FROM {t} WHERE {q(time)} IS NOT NULL ORDER BY {q(time)} DESC "
                f"{self.d.limit(CANDIDATE_ROWS)}",
                [],
            )
        else:
            picked = self.fetch(
                f"SELECT {q(group)} FROM {t} WHERE {q(group)} IS NOT NULL ORDER BY {q(group)} DESC {self.d.limit(1)}", []
            )
        keys = []
        for r in picked:
            if r[0] is not None and r[0] not in keys and len(keys) < CANDIDATES:
                keys.append(r[0])

        select = f"SELECT {q(time)}, {q(group)}, {q(self.column)} FROM {t} WHERE {q(group)} = {self.ph(1)}"
        ranged = f"{select} AND {q(time)} >= {self.ph(2)} AND {q(time)} < {self.ph(3)} ORDER BY {q(time)}"
        hours = self.offset[time] if self.kinds[time] == "datetime" else 0
        chosen = None
        for key in keys:
            recent = self.fetch(
                f"{select} AND {q(time)} IS NOT NULL ORDER BY {q(time)} DESC {self.d.limit(RECENT_ROWS)}", [key]
            )
            if not recent:
                continue
            days = _local_days(recent[0][0], hours)
            oldest = days[-1]
            first_old = self.fetch(f"{ranged} ASC {self.d.limit(1)}", [key, oldest[1], oldest[2]])
            if chosen is None:
                chosen = (key, recent, days, first_old)
            # 가장 오래된 날에도 기록이 있고 그 뒤로 값이 바뀐 묶음이어야 계산 방식이 보인다
            # (방금 기록을 시작했거나 값이 그대로인 묶음은 건너뛴다)
            if first_old and first_old[0][2] != recent[0][2]:
                chosen = (key, recent, days, first_old)
                break
        if chosen is None:
            return ValueSample(index=ix["name"], queries=self.queries, note="테이블이 비어 값 샘플이 없습니다")
        key, recent, days, first_old = chosen
        latest = recent[0][0]
        local = _as_datetime(latest) + datetime.timedelta(hours=hours)
        labeled = []
        for r in recent:
            labeled.append(("최근", r))
        for r in first_old:
            labeled.append((f"{days[-1][0]} 첫 값", r))

        # 현지 날짜마다 첫 값 · 마지막 값 — (묶음 = 값, 시각 범위) 라 인덱스 범위 조회다
        for day, start, end in days:
            if day != days[-1][0]:
                for r in self.fetch(f"{ranged} ASC {self.d.limit(1)}", [key, start, end]):
                    labeled.append((f"{day} 첫 값", r))
            for r in self.fetch(f"{ranged} DESC {self.d.limit(1)}", [key, start, end]):
                labeled.append((f"{day} 마지막 값", r))

        rows, seen = [], set()
        for label, r in sorted(labeled, key=lambda pair: _sort_key(pair[1][0])):
            printed = _shown(r)
            if tuple(printed) in seen:
                continue
            seen.add(tuple(printed))
            rows.append([label] + printed)
        where = f"{group} = {_show(key)}"
        tz = f", 현지 = UTC {hours:+d}시간" if hours else ""
        return ValueSample(
            index=ix["name"],
            queries=self.queries,
            columns=["구분", time, group, self.column],
            rows=rows,
            latest=_show(local),
            note=(
                f"인덱스 {ix['name']} ({', '.join(ix['columns'])}) 로 {where} 의 최근 {RECENT_ROWS}행과 "
                f"최근 {SAMPLE_DAYS}일의 날짜별 첫 값 · 마지막 값을 읽었습니다{tz}"
            ),
        )

    def recent(self, ix: dict, order: str, indexes: list) -> ValueSample:
        q, t = self.d.q, self.d.q(self.table["name"])
        time = order if self.kinds[order] else None
        if time is None:
            for kind in ("datetime", "date"):
                for c in self.table["columns"]:
                    if time is None and self.kinds[c["name"]] == kind:
                        time = c["name"]
        # 묶음 후보: 다른 인덱스의 앞 칸 (대개 장비 · 차량 키)
        groups = []
        for other in indexes:
            head = other["columns"][0]
            if not self.kinds[head] and head not in (self.column, order) and head not in groups:
                groups.append(head)
        shown = []
        for c in [order, time] + groups[:3] + [self.column]:
            if c and c not in shown:
                shown.append(c)
        quoted = []
        for c in shown:
            quoted.append(q(c))
        fetched = self.fetch(
            f"SELECT {', '.join(quoted)} FROM {t} WHERE {q(order)} IS NOT NULL "
            f"ORDER BY {q(order)} DESC {self.d.limit(SCAN_ROWS)}",
            [],
        )
        if not fetched:
            return ValueSample(index=ix["name"], queries=self.queries, note="테이블이 비어 값 샘플이 없습니다")

        # 여러 행이 같은 값을 갖는 첫 묶음 후보로, 가장 많이 나온 값 하나의 행만 남긴다
        rows, where = fetched, ""
        for g in groups[:3]:
            at = shown.index(g)
            counts: dict = {}
            for r in fetched:
                counts[r[at]] = counts.get(r[at], 0) + 1
            if len(counts) < len(fetched):
                key = max(counts, key=counts.get)
                rows = []
                for r in fetched:
                    if r[at] == key:
                        rows.append(r)
                where = f" 중 {g} = {_show(key)}"
                break
        if time is not None:
            at = shown.index(time)
            rows = sorted(rows, key=lambda r: _sort_key(r[at]))
        rows = rows[-SHOW_ROWS:]
        latest = None
        if time is not None:
            hours = self.offset[time] if self.kinds[time] == "datetime" else 0
            latest = _show(_as_datetime(rows[-1][shown.index(time)]) + datetime.timedelta(hours=hours))
        printed = []
        for r in rows:
            printed.append(_shown(r))
        return ValueSample(
            index=ix["name"],
            queries=self.queries,
            columns=shown,
            rows=printed,
            latest=latest,
            note=f"인덱스 {ix['name']} ({', '.join(ix['columns'])}) 순서로 최근 {SCAN_ROWS}행{where}을 읽었습니다",
        )

    def ph(self, n: int) -> str:
        return f":{n}" if self.d.driver == "oracle" else "%s"

    def fetch(self, sql: str, params: list) -> list:
        shown = sql
        if params:
            shown += "  -- " + ", ".join(_shown(params))
        self.queries.append(shown)
        self.check_plan(sql, params)
        self.cur.execute(sql, params or None)
        return self.cur.fetchall()

    def check_plan(self, sql: str, params: list) -> None:
        scanned = full_scans(self.cur, self.d, sql, params)
        if scanned:
            raise _FullScan(f"실행 계획이 {', '.join(scanned)} 전체를 훑어 값 샘플을 건너뛰었습니다")


def _local_days(latest, hours: int) -> list[tuple]:
    """latest 의 현지 날짜부터 SAMPLE_DAYS 일 → [(현지 날짜, 시작, 끝)] (시작 · 끝은 저장된 시각 기준, 최근 날이 먼저)"""
    local = _as_datetime(latest) + datetime.timedelta(hours=hours)
    days = []
    for i in range(SAMPLE_DAYS):
        day = local.date() - datetime.timedelta(days=i)
        start = datetime.datetime.combine(day, datetime.time()) - datetime.timedelta(hours=hours)
        if getattr(latest, "tzinfo", None) is not None:
            start = start.replace(tzinfo=latest.tzinfo)
        days.append((day, start, start + datetime.timedelta(days=1)))
    return days


def _as_datetime(value) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time())
    return datetime.datetime.min


# ── 실행 계획 확인 ─────────────────────────────────────────────────────────


def full_scans(cur, dialect: Dialect, sql: str, params: list | None = None) -> list[str]:
    """실행 계획에서 전체를 훑는 큰 테이블 (PostgreSQL Seq Scan, MySQL type ALL). 실행하지 않고 EXPLAIN 만 본다.

    작은 테이블(SMALL_TABLE_ROWS 미만)은 뺀다 — 작으면 인덱스가 있어도 전체 훑기가 더 싸다.
    Oracle 은 EXPLAIN PLAN 이 PLAN_TABLE 에 써야 해서 읽기 전용 트랜잭션에서 볼 수 없다 — 빈 목록이다.
    """
    found = []
    if dialect.driver == "postgresql":
        cur.execute(f"EXPLAIN (FORMAT JSON) {sql}", params or None)
        plan = cur.fetchone()[0]
        if isinstance(plan, str):
            plan = json.loads(plan)
        scanned = []
        _seq_scans(plan[0]["Plan"], scanned)
        for name in scanned:
            cur.execute("SELECT reltuples FROM pg_class WHERE oid = to_regclass(%s)", [dialect.q(name)])
            rows = cur.fetchone()
            # 통계가 없으면(-1) 큰 테이블로 본다
            if (not rows or rows[0] is None or rows[0] < 0 or rows[0] >= SMALL_TABLE_ROWS) and name not in found:
                found.append(name)
    elif dialect.driver == "mysql":
        cur.execute(f"EXPLAIN {sql}", params or None)
        names = []
        for d in cur.description:
            names.append(str(d[0]).lower())
        for r in cur.fetchall():
            table = r[names.index("table")]
            if r[names.index("type")] == "ALL" and (r[names.index("rows")] or 0) >= SMALL_TABLE_ROWS and table not in found:
                found.append(table)
    return found


def _seq_scans(node: dict, out: list) -> None:
    if node.get("Node Type") == "Seq Scan" and node.get("Relation Name"):
        out.append(node["Relation Name"])
    for child in node.get("Plans") or []:
        _seq_scans(child, out)


def _sort_key(value) -> datetime.datetime:
    """시각 정렬용. 한 컬럼의 값끼리만 비교하므로 시간대는 떼고, 비어 있으면 맨 앞이다."""
    return _as_datetime(value).replace(tzinfo=None)


def _shown(values) -> list[str]:
    out = []
    for v in values:
        out.append(_show(v))
    return out


def _show(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, datetime.datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."
