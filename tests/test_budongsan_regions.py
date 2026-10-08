import os

import psycopg
import pytest
import requests

from budongsan import Config, CollectionError, init_schema
from budongsan_regions import (
    LOCK_ID, RegionClient, RegionCodeError, parse_page, save_codes, select_regions,
)


def region(code, name):
    return dict(region_cd=code, sido_cd=code[:2], sgg_cd=code[2:5],
                umd_cd=code[5:8], ri_cd=code[8:], locatadd_nm=name)


ROWS = [
    region('1100000000', '서울특별시'), region('1111000000', '서울특별시 종로구'),
    region('1111010100', '서울특별시 종로구 청운동'),
    region('4100000000', '경기도'), region('4111000000', '경기도 수원시'),
    region('4111100000', '경기도 수원시 장안구'), region('4111110100', '경기도 수원시 장안구 영화동'),
    region('3611000000', '세종특별자치시'), region('3611010100', '세종특별자치시 반곡동'),
]


def payload(rows, total=None, page=1, size=1000, code='INFO-0'):
    return {'StanReginCd': [
        {'head': [{'totalCount': total if total is not None else len(rows)},
                  {'numOfRows': str(size), 'pageNo': str(page)}, {'RESULT': {'resultCode': code}}]},
        {'row': rows},
    ]}


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def client(*responses):
    return RegionClient('hidden-key', Session(responses), sleep=lambda _: None)


def test_real_envelope_and_pagination():
    c = client(Response(payload(ROWS[:2], total=3, size=2)),
               Response(payload(ROWS[2:3], total=3, size=2, page=2)))
    assert c.fetch_all() == ROWS[:3]
    assert c.session.calls[0]['params']['ServiceKey'] == 'hidden-key'


@pytest.mark.parametrize('data', [payload([], total=0), payload(ROWS, code='ERROR'), {},
                               {'OpenAPI_ServiceResponse': {'cmmMsgHeader': {'returnReasonCode': '30'}}}])
def test_errors_not_treated_as_empty_catalog(data):
    with pytest.raises(RegionCodeError):
        parse_page(data)


@pytest.mark.parametrize('second', [payload(ROWS[2:3], total=4, size=2, page=2),
                                   payload([], total=3, size=2, page=2),
                                   payload(ROWS[:1], total=3, size=2, page=2)])
def test_changing_missing_or_duplicate_rows_rejected(second):
    c = client(Response(payload(ROWS[:2], total=3, size=2)), Response(second))
    with pytest.raises(RegionCodeError):
        c.fetch_all()


def test_transient_retry_and_secret_redaction():
    c = client(requests.Timeout('hidden-key'), Response(None, 503), Response(payload(ROWS)))
    assert c.fetch_all() == ROWS
    c = client(*(requests.Timeout('hidden-key') for _ in range(4)))
    with pytest.raises(RegionCodeError) as e:
        c.fetch_all()
    assert 'hidden-key' not in str(e.value)


def test_name_config_does_not_default_to_nationwide(monkeypatch):
    monkeypatch.setenv('DB_URL', 'postgresql://example')
    monkeypatch.setenv('BUDONGSAN_DB_URL', '')
    monkeypatch.setenv('MOLIT_SERVICE_KEY', 'hidden-key')
    monkeypatch.setenv('MOIS_SERVICE_KEY', 'another-hidden-key')
    monkeypatch.setenv('BUDONGSAN_REGIONS', '서울특별시')
    monkeypatch.setenv('BUDONGSAN_LAWD_CODES', '')
    cfg = Config.from_env()
    assert cfg.region_names == ('서울특별시',) and cfg.regions == ()
    assert 'hidden-key' not in repr(cfg)
    monkeypatch.setenv('BUDONGSAN_LAWD_CODES', '11110')
    with pytest.raises(CollectionError):
        Config.from_env()
    monkeypatch.setenv('BUDONGSAN_LAWD_CODES', '')
    monkeypatch.setenv('BUDONGSAN_REGIONS', '')
    with pytest.raises(CollectionError):
        Config.from_env()


@pytest.fixture
def catalog():
    url = os.getenv('BUDONGSAN_TEST_DB_URL')
    if not url:
        pytest.skip('BUDONGSAN_TEST_DB_URL is not set')
    with psycopg.connect(url) as conn:
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
        init_schema(conn)
        save_codes(conn, ROWS)
        yield conn
        conn.rollback()


def test_pg_resolve_province_city_ward_and_sejong(catalog):
    assert select_regions(catalog, ['서울특별시']) == ('11110',)
    assert select_regions(catalog, ['경기도']) == ('41111',)
    assert select_regions(catalog, ['경기도 수원시']) == ('41111',)
    assert select_regions(catalog, ['세종특별자치시']) == ('36110',)
    assert select_regions(catalog, ['전국']) == ('11110', '36110', '41111')
    assert select_regions(catalog, ['서울특별시', '서울특별시 종로구']) == ('11110',)
    with pytest.raises(RegionCodeError):
        select_regions(catalog, ['서울'])
    with pytest.raises(RegionCodeError):
        select_regions(catalog, ['서울특별시 종로구 청운동'])


def test_pg_upsert_missing_preserved_and_reactivated(catalog):
    save_codes(catalog, ROWS[:-2])
    assert catalog.execute("SELECT is_current FROM budongsan.region_code WHERE region_cd='3611000000'").fetchone() == (False,)
    save_codes(catalog, ROWS)
    save_codes(catalog, ROWS)
    assert catalog.execute('SELECT count(*) FROM budongsan.region_code WHERE is_current').fetchone()[0] == len(ROWS)
    assert select_regions(catalog, ['세종특별자치시']) == ('36110',)


def test_pg_failed_snapshot_does_not_change_current_codes(catalog):
    with pytest.raises(RegionCodeError):
        save_codes(catalog, [])
    with pytest.raises(RegionCodeError):
        save_codes(catalog, [ROWS[0], ROWS[0]])
    # Fail during staging after validation. Current snapshot must survive.
    invalid = {**ROWS[0], 'locatadd_nm': 'bad\x00name'}
    with pytest.raises(psycopg.Error):
        save_codes(catalog, [invalid])
    assert select_regions(catalog, ['전국']) == ('11110', '36110', '41111')
