from datetime import date
from decimal import Decimal
import os

import psycopg
import pytest
import requests

from budongsan import (
    CollectionError, Config, LOCK_ID, MolitClient, init_schema,
    month_range, normalize, optional_date, parse_page, recent_months, save_partition,
)


def trade(**changes):
    return {'sggCd': '11110', 'aptNm': '테스트아파트', 'umdNm': '테스트동',
            'jibun': '1-1', 'excluUseAr': '84.9300', 'dealYear': '2026',
            'dealMonth': '01', 'dealDay': '02', 'dealAmount': '120,000',
            'floor': '-1', 'buildYear': '2000', **changes}


def xml(items=(), total=0, page=1, size=1000, code='000'):
    rows = ''.join('<item>' + ''.join(f'<{k}>{v}</{k}>' for k, v in item.items()) + '</item>' for item in items)
    return (f'<response><header><resultCode>{code}</resultCode></header><body>'
            f'<items>{rows}</items><totalCount>{total}</totalCount><pageNo>{page}</pageNo>'
            f'<numOfRows>{size}</numOfRows></body></response>').encode()


class Response:
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status


class Session:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def client(*responses):
    return MolitClient('secret+/=', Session(*responses), sleep=lambda _: None)


def test_months_cross_year():
    assert recent_months(3, date(2026, 1, 31)) == ['202511', '202512', '202601']
    assert month_range('202512', '202601') == ['202512', '202601']


@pytest.mark.parametrize('start,end', [('202613', '202701'), ('202601', '202501'), ('26-01', '202601')])
def test_bad_months(start, end):
    with pytest.raises(CollectionError):
        month_range(start, end)


def test_config_key_decode_and_regions(monkeypatch):
    monkeypatch.setenv('DB_URL', 'postgresql://example')
    monkeypatch.setenv('BUDONGSAN_DB_URL', '')
    monkeypatch.setenv('MOLIT_SERVICE_KEY', 'secret%2B%2F%3D')
    monkeypatch.setenv('BUDONGSAN_LAWD_CODES', '11110, 11680,11110')
    monkeypatch.setenv('BUDONGSAN_REGIONS', '')
    monkeypatch.setenv('BUDONGSAN_LOOKBACK_MONTHS', '3')
    cfg = Config.from_env()
    assert cfg.service_key == 'secret+/='
    assert cfg.regions == ('11110', '11680')
    assert 'secret' not in repr(cfg) and 'postgresql' not in repr(cfg)
    monkeypatch.setenv('BUDONGSAN_LAWD_CODES', '11')
    with pytest.raises(CollectionError):
        Config.from_env()
    monkeypatch.setenv('MOLIT_SERVICE_KEY', '')
    assert Config.from_env(database_only=True).db_url == 'postgresql://example'


def test_all_pages_preserve_identical_trades():
    c = client(Response(xml([trade(), trade()], total=3, size=2)),
               Response(xml([trade()], total=3, size=2, page=2)))
    assert len(c.fetch('11110', '202601')) == 3
    assert [x['params']['pageNo'] for x in c.session.calls] == [1, 2]
    assert c.session.calls[0]['params']['serviceKey'] == 'secret+/='


def test_valid_empty_response():
    assert client(Response(xml())).fetch('11110', '202601') == []


@pytest.mark.parametrize('second', [
    xml([], total=2, page=2, size=1),
    xml([trade()], total=3, page=2, size=1),
    xml([trade()], total=2, page=1, size=1),
])
def test_incomplete_or_changing_pages_rejected(second):
    c = client(Response(xml([trade()], total=2, size=1)), Response(second))
    with pytest.raises(CollectionError):
        c.fetch('11110', '202601')


def test_retry_transient_network_http_and_api():
    c = client(requests.Timeout('URL contains secret'), Response(b'', 503),
               Response(xml(code='23')), Response(xml()))
    assert c.fetch('11110', '202601') == []
    assert len(c.session.calls) == 4


def test_network_failure_does_not_expose_key():
    c = client(*(requests.Timeout('URL contains secret') for _ in range(4)))
    with pytest.raises(CollectionError) as error:
        c.fetch('11110', '202601')
    assert 'secret' not in str(error.value)


@pytest.mark.parametrize('content', [
    b'<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>',
    b'<html>secret</html>', b'invalid',
    b'<!DOCTYPE response [<!ENTITY x "expanded">]><response>&x;</response>',
])
def test_error_envelopes_and_invalid_xml(content):
    with pytest.raises(CollectionError):
        parse_page(content)


def test_auth_error_is_not_retried():
    c = client(Response(xml(code='30')))
    with pytest.raises(CollectionError):
        c.fetch('11110', '202601')
    assert len(c.session.calls) == 1


def test_normalize_types_cancellation_dates_and_raw():
    row = normalize(trade(cdealType='O', cdealDay='26.02.03', rgstDate='20260204'), '11110', '202601')
    assert row[4] == Decimal('84.9300')
    assert row[5] == -1
    assert row[7:12] == (date(2026, 1, 2), 120000, True, date(2026, 2, 3), date(2026, 2, 4))
    assert optional_date('-') is None


@pytest.mark.parametrize('changes', [{'sggCd': '11680'}, {'dealMonth': '02'},
                                    {'dealAmount': ''}, {'excluUseAr': 'NaN'}, {'cdealType': '?'}])
def test_invalid_record_rejected(changes):
    with pytest.raises(CollectionError):
        normalize(trade(**changes), '11110', '202601')


@pytest.fixture
def pg():
    url = os.getenv('BUDONGSAN_TEST_DB_URL')
    if not url:
        pytest.skip('BUDONGSAN_TEST_DB_URL is not set')
    # Every change, including DDL, is rolled back. Coordinate with real collectors.
    with psycopg.connect(url) as conn:
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
        init_schema(conn)
        yield conn
        conn.rollback()


def new_run(pg):
    return pg.execute("""INSERT INTO budongsan.collection_run(status, regions, months)
                         VALUES ('running', ARRAY['11110'], ARRAY['202601']) RETURNING id""").fetchone()[0]


def test_pg_rerun_cancellation_duplicates_and_zero(pg):
    run = new_run(pg)
    count_sql = 'SELECT count(*) FROM budongsan.apartment_trade WHERE collection_run_id=%s'
    save_partition(pg, run, '11110', '202601', [trade(), trade()])
    assert pg.execute(count_sql, (run,)).fetchone()[0] == 2
    save_partition(pg, run, '11110', '202601', [trade(), trade()])
    assert pg.execute(count_sql, (run,)).fetchone()[0] == 2
    save_partition(pg, run, '11110', '202601', [trade(cdealType='O')])
    assert pg.execute('SELECT is_cancelled FROM budongsan.apartment_trade WHERE collection_run_id=%s', (run,)).fetchone()[0]
    save_partition(pg, run, '11110', '202601', [])
    assert pg.execute(count_sql, (run,)).fetchone()[0] == 0


def test_pg_validation_and_insert_failure_preserve_snapshot(pg):
    run = new_run(pg)
    save_partition(pg, run, '11110', '202601', [trade()])
    with pytest.raises(CollectionError):
        save_partition(pg, run, '11110', '202601', [trade(dealAmount='bad')])
    # Valid integer in Python but too large for PostgreSQL bigint: tests rollback AFTER delete.
    with pytest.raises(psycopg.DataError):
        save_partition(pg, run, '11110', '202601', [trade(dealAmount='9' * 40)])
    assert pg.execute('SELECT deal_amount_manwon FROM budongsan.apartment_trade WHERE collection_run_id=%s', (run,)).fetchall() == [(120000,)]
