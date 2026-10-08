"""Official MOIS legal-district code sync and MOLIT region selection."""
import logging
import re
import time

import requests
from psycopg.types.json import Jsonb

API_URL = 'https://apis.data.go.kr/1741000/StanReginCd/getStanReginCdList'
LOCK_ID = 620061612
log = logging.getLogger('budongsan')


class RegionCodeError(RuntimeError):
    """Messages must not contain API keys or raw server responses."""


class RetryableRegionError(RegionCodeError):
    pass


def parse_page(payload):
    try:
        if 'OpenAPI_ServiceResponse' in payload:
            code = str(payload['OpenAPI_ServiceResponse']['cmmMsgHeader']['returnReasonCode'])
            safe = code if re.fullmatch(r'[0-9]{1,3}', code) else 'unknown'
            error = RetryableRegionError if safe in ('01', '04', '05', '23') else RegionCodeError
            raise error(f'Region API error {safe}; check service approval/key/quota.')
        blocks = payload['StanReginCd']
        head = {k: v for block in blocks for entry in block.get('head', []) for k, v in entry.items()}
        if head['RESULT']['resultCode'] != 'INFO-0':
            raise RegionCodeError('Region API reported an unsuccessful result.')
        rows = [row for block in blocks for row in block.get('row', [])]
        total, page, size = int(head['totalCount']), int(head['pageNo']), int(head['numOfRows'])
        if total < 1 or page < 1 or size < 1:
            raise ValueError
        return rows, total, page, size
    except (KeyError, TypeError, ValueError, AttributeError):
        raise RegionCodeError('Invalid region API response; existing codes retained.') from None


def validate_rows(rows):
    seen = set()
    if not rows:
        raise RegionCodeError('Empty region snapshot; existing codes retained.')
    for row in rows:
        code = row.get('region_cd', '')
        if (not isinstance(code, str) or not re.fullmatch(r'[0-9]{10}', code)
                or code in seen or not isinstance(row.get('locatadd_nm'), str)
                or not row['locatadd_nm'].strip()
                or code != ''.join(str(row.get(k, '')) for k in ('sido_cd', 'sgg_cd', 'umd_cd', 'ri_cd'))):
            raise RegionCodeError('Invalid or duplicate region code; existing codes retained.')
        seen.add(code)


class RegionClient:
    def __init__(self, key, session=None, sleep=time.sleep):
        self.key = key
        self.session = session or requests.Session()
        self.sleep = sleep

    def close(self):
        self.session.close()

    def _page(self, page):
        for attempt in range(4):
            self.sleep(0.2 if attempt == 0 else 2 ** attempt)
            try:
                response = self.session.get(API_URL, params={
                    'ServiceKey': self.key, 'pageNo': page, 'numOfRows': 1000, 'type': 'json',
                }, timeout=(10, 60))
                if response.status_code == 429 or response.status_code >= 500:
                    raise RetryableRegionError(f'Region API HTTP {response.status_code}.')
                if response.status_code != 200:
                    raise RegionCodeError(f'Region API HTTP {response.status_code}; check service approval/key.')
                try:
                    payload = response.json()
                except ValueError:
                    raise RegionCodeError('Region API did not return JSON.') from None
                return parse_page(payload)
            except requests.RequestException:
                failure = RetryableRegionError('Region API network request failed.')
            except RetryableRegionError as exc:
                failure = exc
            if attempt == 3:
                raise failure from None

    def fetch_all(self):
        rows = []
        expected = None
        page = 1
        while True:
            items, total, returned_page, size = self._page(page)
            if expected is None:
                expected = (total, size)
            if expected != (total, size) or returned_page != page or len(items) != min(size, total - len(rows)):
                raise RegionCodeError('Incomplete or changed region pages; existing codes retained.')
            rows.extend(items)
            if len(rows) == total:
                validate_rows(rows)
                return rows
            page += 1


def save_codes(conn, rows):
    validate_rows(rows)
    with conn.transaction():
        # Stage the complete response before changing the current catalog.
        conn.execute('''CREATE TEMP TABLE region_code_stage (
            region_cd text PRIMARY KEY, address_name text NOT NULL,
            parent_code text, raw_data jsonb NOT NULL) ON COMMIT DROP''')
        with conn.cursor() as cur:
            with cur.copy('COPY region_code_stage (region_cd, address_name, parent_code, raw_data) FROM STDIN') as copy:
                for row in rows:
                    copy.write_row((row['region_cd'], row['locatadd_nm'].strip(), row.get('locathigh_cd') or None, Jsonb(row)))
        conn.execute('''INSERT INTO budongsan.region_code(region_cd, address_name, parent_code, raw_data)
                        SELECT region_cd, address_name, parent_code, raw_data FROM region_code_stage
                        ON CONFLICT (region_cd) DO UPDATE SET
                            address_name=EXCLUDED.address_name, parent_code=EXCLUDED.parent_code,
                            raw_data=EXCLUDED.raw_data, is_current=true, last_seen_at=now()''')
        conn.execute('''UPDATE budongsan.region_code SET is_current=false
                        WHERE is_current AND NOT EXISTS (
                            SELECT 1 FROM region_code_stage s WHERE s.region_cd=region_code.region_cd)''')
        conn.execute('DROP TABLE region_code_stage')


def sync_codes(conn, key, client=None):
    if not key:
        raise RegionCodeError('Set MOIS_SERVICE_KEY for the region code API.')
    if not conn.execute('SELECT pg_try_advisory_lock(%s)', (LOCK_ID,)).fetchone()[0]:
        raise RegionCodeError('Another region sync is running; retry later.')
    owned = client is None
    client = client or RegionClient(key)
    try:
        conn.execute("""UPDATE budongsan.region_sync_run SET status='failed', finished_at=now(),
                        error='Region sync interrupted' WHERE status='running'""")
        run_id = conn.execute("INSERT INTO budongsan.region_sync_run(status) VALUES ('running') RETURNING id").fetchone()[0]
        try:
            rows = client.fetch_all()
            # Catalog changes and success status commit together.
            with conn.transaction():
                save_codes(conn, rows)
                conn.execute("""UPDATE budongsan.region_sync_run SET status='success', finished_at=now(),
                                row_count=%s WHERE id=%s""", (len(rows), run_id))
        except Exception as exc:
            detail = str(exc) if isinstance(exc, RegionCodeError) else type(exc).__name__
            conn.execute("""UPDATE budongsan.region_sync_run SET status='failed', finished_at=now(), error=%s
                            WHERE id=%s""", (detail, run_id))
            raise RegionCodeError(f'Region sync failed: {detail}') from None
        log.info('Region catalog synchronized: %s codes', len(rows))
        return len(rows)
    finally:
        try:
            if owned:
                client.close()
        finally:
            conn.execute('SELECT pg_advisory_unlock(%s)', (LOCK_ID,))


def select_regions(conn, names):
    """Match complete administrative names, and select their leaf query districts.

    Selecting a city with non-autonomous wards includes those wards, not its
    parent-city code. Sejong also works without assuming a name word count.
    """
    districts = dict(conn.execute('SELECT lawd_cd, address_name FROM budongsan.trade_region ORDER BY lawd_cd').fetchall())
    if not districts:
        raise RegionCodeError('Region catalog is empty; run --sync-regions first.')
    selected = set()
    for name in names:
        if name in ('전국', 'ALL'):
            selected.update(districts)
            continue
        parents = conn.execute('''SELECT region_cd FROM budongsan.region_code
                                  WHERE is_current AND address_name=%s AND right(region_cd,5)='00000' ''', (name,)).fetchall()
        if len(parents) != 1:
            raise RegionCodeError('Unknown or ambiguous region name; use its full official name.')
        matches = {code for code, label in districts.items() if label == name or label.startswith(name + ' ')}
        if not matches:
            raise RegionCodeError('No trade districts found for the selected region.')
        selected.update(matches)
    return tuple(sorted(selected))
