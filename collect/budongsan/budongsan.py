"""MOLIT apartment sales collector, independent of the web/LLM process."""
from __future__ import annotations

import argparse
import logging
from logging.handlers import TimedRotatingFileHandler
import os
from pathlib import Path
import re
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import unquote

from defusedxml import ElementTree
from dotenv import load_dotenv
import psycopg
from psycopg.types.json import Jsonb
import requests

from budongsan_regions import RegionCodeError, select_regions, sync_codes

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parents[1]
API_URL = 'https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade'
KST = timezone(timedelta(hours=9), 'Asia/Seoul')
LOCK_ID = 620061610
log = logging.getLogger('budongsan')


class CollectionError(RuntimeError):
    """Safe to log: never include HTTP URLs, response bodies, or credentials."""


class TransientError(CollectionError):
    pass


def month_range(start: str, end: str) -> list[str]:
    for value in (start, end):
        if not re.fullmatch(r'[0-9]{6}', value):
            raise CollectionError('Months must use YYYYMM.')
        try:
            date(int(value[:4]), int(value[4:]), 1)
        except ValueError:
            raise CollectionError('Invalid calendar month.') from None
    if start > end:
        raise CollectionError('Start month must not be after end month.')
    first = int(start[:4]) * 12 + int(start[4:]) - 1
    last = int(end[:4]) * 12 + int(end[4:]) - 1
    return [f'{n // 12:04d}{n % 12 + 1:02d}' for n in range(first, last + 1)]


def recent_months(count: int, today: date | None = None) -> list[str]:
    today = today or datetime.now(KST).date()
    last = today.year * 12 + today.month - 1
    first = last - count + 1
    return month_range(f'{first // 12:04d}{first % 12 + 1:02d}', today.strftime('%Y%m'))


@dataclass(frozen=True)
class Config:
    db_url: str = field(repr=False)
    service_key: str = field(repr=False)
    regions: tuple[str, ...]
    lookback_months: int = 3
    region_names: tuple[str, ...] = ()
    region_service_key: str = field(default='', repr=False)

    @classmethod
    def from_env(cls, *, database_only: bool = False) -> Config:
        db_url = os.getenv('BUDONGSAN_DB_URL', '').strip() or os.getenv('DB_URL', '').strip()
        key = unquote(os.getenv('MOLIT_SERVICE_KEY', '').strip())
        regions = tuple(dict.fromkeys(x.strip() for x in os.getenv('BUDONGSAN_LAWD_CODES', '').split(',') if x.strip()))
        names = tuple(dict.fromkeys(x.strip() for x in os.getenv('BUDONGSAN_REGIONS', '').split(',') if x.strip()))
        region_key = unquote(os.getenv('MOIS_SERVICE_KEY', '').strip())
        if not db_url:
            raise CollectionError('Set DB_URL or BUDONGSAN_DB_URL in backend/.env.')
        if not database_only:
            if not key:
                raise CollectionError('Set MOLIT_SERVICE_KEY in backend/.env.')
            if not (regions or names) or any(not re.fullmatch(r'[0-9]{5}', x) for x in regions):
                raise CollectionError('Set BUDONGSAN_REGIONS or BUDONGSAN_LAWD_CODES in backend/.env.')
            if names and regions:
                raise CollectionError('Use either BUDONGSAN_REGIONS or BUDONGSAN_LAWD_CODES, not both.')
            if names and not region_key:
                raise CollectionError('Set MOIS_SERVICE_KEY to resolve BUDONGSAN_REGIONS.')
        try:
            lookback = int(os.getenv('BUDONGSAN_LOOKBACK_MONTHS', '3'))
            if not 1 <= lookback <= 240:
                raise ValueError
        except ValueError:
            raise CollectionError('BUDONGSAN_LOOKBACK_MONTHS must be 1..240.') from None
        return cls(db_url, key, regions, lookback, names, region_key)


def parse_page(content: bytes) -> tuple[list[dict[str, str]], int, int, int]:
    try:
        root = ElementTree.fromstring(content)
    except Exception:
        raise CollectionError('API returned invalid or unsafe XML.') from None
    code = root.findtext('./header/resultCode')
    if code is None:
        code = root.findtext('.//returnReasonCode')
    if code not in ('000', '00', '0'):
        safe_code = code if code and re.fullmatch(r'[0-9]{1,3}', code) else 'unknown'
        error = TransientError if safe_code in ('01', '04', '05', '23') else CollectionError
        raise error(f'MOLIT API error code {safe_code}; check service approval, key and quota.')
    try:
        total = int(root.findtext('./body/totalCount', ''))
        page = int(root.findtext('./body/pageNo', ''))
        size = int(root.findtext('./body/numOfRows', ''))
        if total < 0 or page < 1 or size < 1:
            raise ValueError
    except ValueError:
        raise CollectionError('Missing or invalid API pagination metadata.') from None
    items = [{node.tag: (node.text or '').strip() for node in item}
             for item in root.findall('./body/items/item')]
    return items, total, page, size


class MolitClient:
    def __init__(self, service_key: str, session=None, sleep=time.sleep):
        self.key = service_key
        self.session = session or requests.Session()
        self.sleep = sleep

    def close(self):
        self.session.close()

    def _page(self, region: str, month: str, page: int):
        for attempt in range(4):
            self.sleep(0.2 if attempt == 0 else 2 ** attempt)
            try:
                response = self.session.get(API_URL, params={
                    'serviceKey': self.key, 'LAWD_CD': region, 'DEAL_YMD': month,
                    'pageNo': page, 'numOfRows': 1000,
                }, timeout=(10, 60))
                if response.status_code == 429 or response.status_code >= 500:
                    raise TransientError(f'API HTTP {response.status_code}.')
                if response.status_code != 200:
                    raise CollectionError(f'API HTTP {response.status_code}; check API access.')
                return parse_page(response.content)
            except requests.RequestException:
                failure = TransientError('API network request failed.')
            except TransientError as exc:
                failure = exc
            if attempt == 3:
                raise failure from None

    def fetch(self, region: str, month: str) -> list[dict[str, str]]:
        rows = []
        expected_total = None
        expected_size = None
        page = 1
        while True:
            items, total, returned_page, size = self._page(region, month, page)
            if expected_total is None:
                expected_total, expected_size = total, size
            if total != expected_total or size != expected_size or returned_page != page:
                raise CollectionError('Pagination changed during collection; existing data retained.')
            expected_count = min(size, total - len(rows))
            if len(items) != expected_count:
                raise CollectionError('Incomplete API page; existing data retained.')
            rows.extend(items)
            if len(rows) == total:
                return rows
            page += 1


def optional_date(value: str | None) -> date | None:
    if not value or value == '-':
        return None
    for fmt in ('%Y%m%d', '%Y-%m-%d', '%y.%m.%d', '%Y.%m.%d'):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise CollectionError('Invalid optional date in API record.')


def normalize(item: dict[str, str], region: str, month: str) -> tuple:
    try:
        deal_date = date(int(item['dealYear']), int(item['dealMonth']), int(item['dealDay']))
        amount = int(item['dealAmount'].replace(',', ''))
        area = Decimal(item['excluUseAr'])
        if (item['sggCd'] != region or deal_date.strftime('%Y%m') != month
                or not item['aptNm'] or amount <= 0 or not area.is_finite() or area <= 0):
            raise ValueError
        cancelled = item.get('cdealType', '')
        if cancelled not in ('', '-', 'N', 'O', 'Y'):
            raise ValueError
        return (
            item['aptNm'], item.get('umdNm') or None, item.get('jibun') or None,
            item.get('aptDong') or None, area, int(item['floor']) if item.get('floor') else None,
            int(item['buildYear']) if item.get('buildYear') else None,
            deal_date, amount, cancelled in ('O', 'Y'),
            optional_date(item.get('cdealDay')), optional_date(item.get('rgstDate')),
            item.get('dealingGbn') or None, item.get('estateAgentSggNm') or None,
            item.get('slerGbn') or None, item.get('buyerGbn') or None,
            item.get('landLeaseholdGbn') or None, Jsonb(item),
        )
    except (KeyError, ValueError, ArithmeticError):
        raise CollectionError('Invalid API record; existing region/month data retained.') from None


def init_schema(conn):
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID + 1,))
        conn.execute((BASE_DIR / 'budongsan_schema.sql').read_text(encoding='utf-8'))


def save_partition(conn, run_id: int, region: str, month: str, items: list[dict]) -> None:
    # Validate everything before DELETE. Identical rows represent potentially
    # distinct trades and MUST retain their multiplicity.
    values = [(region, month, n, *normalize(item, region, month), run_id)
              for n, item in enumerate(items, 1)]
    with conn.transaction():
        conn.execute('DELETE FROM budongsan.apartment_trade WHERE lawd_cd=%s AND deal_ym=%s', (region, month))
        with conn.cursor() as cur:
            cur.executemany('''INSERT INTO budongsan.apartment_trade (
                lawd_cd, deal_ym, row_no, apartment_name, legal_dong, jibun, apartment_dong,
                exclusive_area_m2, floor, build_year, deal_date, deal_amount_manwon,
                is_cancelled, cancellation_date, registration_date, dealing_type,
                estate_agent_region, seller_type, buyer_type, land_leasehold_type, raw_data,
                collection_run_id) VALUES (''' + ','.join(['%s'] * 22) + ')', values)
        conn.execute('''UPDATE budongsan.collection_run SET partitions_saved=partitions_saved+1,
                        rows_saved=rows_saved+%s WHERE id=%s''', (len(values), run_id))


def collect(config: Config, months: list[str], client=None) -> bool:
    with psycopg.connect(config.db_url, autocommit=True, connect_timeout=10) as conn:
        if not conn.execute('SELECT pg_try_advisory_lock(%s)', (LOCK_ID,)).fetchone()[0]:
            log.info('Another collector is running; skipped.')
            return True
        # Closing this dedicated connection releases the session lock on every path.
        init_schema(conn)
        conn.execute("""UPDATE budongsan.collection_run SET status='failed', finished_at=now(),
                        error='Collector interrupted before completion' WHERE status='running'""")
        run_id = conn.execute('''INSERT INTO budongsan.collection_run(status, regions, months)
                                 VALUES ('running', %s, %s) RETURNING id''',
                              (list(config.regions), months)).fetchone()[0]
        owned_client = client is None
        client = client or MolitClient(config.service_key)
        failures = []
        total = len(config.regions) * len(months)
        completed = 0
        log.info('Collection run=%s started: regions=%s months=%s partitions=%s; sequential execution',
                 run_id, len(config.regions), ','.join(months), total)
        try:
            for region in config.regions:
                for month in months:
                    completed += 1
                    try:
                        items = client.fetch(region, month)
                        save_partition(conn, run_id, region, month, items)
                        log.info('Progress %s/%s: saved region=%s month=%s rows=%s',
                                 completed, total, region, month, len(items))
                    except Exception as exc:
                        # Never stringify arbitrary HTTP/DB exceptions: they can contain secrets.
                        detail = str(exc) if isinstance(exc, CollectionError) else type(exc).__name__
                        message = f'{region}/{month}: {detail}'
                        failures.append(message)
                        log.error('Progress %s/%s: %s', completed, total, message)
            conn.execute('''UPDATE budongsan.collection_run SET status=%s, finished_at=now(), error=%s
                            WHERE id=%s''', ('failed' if failures else 'success', '\n'.join(failures) or None, run_id))
        finally:
            if owned_client:
                client.close()
        log.info('Collection run=%s completed; failed partitions=%s', run_id, len(failures))
        return not failures


def main(argv=None) -> int:
    load_dotenv(ROOT_DIR / 'backend' / '.env')
    load_dotenv(ROOT_DIR / '.env')
    parser = argparse.ArgumentParser(description='Collect MOLIT apartment sales into budongsan.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--init-db', action='store_true', help='Create schema only; no API key required')
    mode.add_argument('--check-config', action='store_true', help='Validate settings without network access')
    mode.add_argument('--sync-regions', action='store_true', help='Refresh all legal-district codes without collecting trades')
    mode.add_argument('--list-regions', action='store_true', help='List cached 5-digit trade regions')
    parser.add_argument('--start-month', help='Backfill first YYYYMM (inclusive)')
    parser.add_argument('--end-month', help='Backfill last YYYYMM (inclusive)')
    args = parser.parse_args(argv)
    log_dir = ROOT_DIR / 'logs'
    log_dir.mkdir(exist_ok=True)
    handler = TimedRotatingFileHandler(log_dir / 'budongsan.log', when='midnight', backupCount=30, encoding='utf-8')
    handlers = [handler]
    if sys.stderr is not None:  # pythonw.exe has no console streams.
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format='%(asctime)s %(levelname)s %(message)s')
    try:
        config = Config.from_env(database_only=args.init_db or args.sync_regions or args.list_regions)
        if bool(args.start_month) != bool(args.end_month):
            raise CollectionError('Provide both --start-month and --end-month.')
        months = month_range(args.start_month, args.end_month) if args.start_month else recent_months(config.lookback_months)
        if args.check_config:
            log.info('Configuration OK: regions=%s months=%s', ','.join(config.region_names or config.regions), ','.join(months))
            return 0
        if args.init_db:
            with psycopg.connect(config.db_url, autocommit=True, connect_timeout=10) as conn:
                init_schema(conn)
            log.info('budongsan schema initialized.')
            return 0
        if args.sync_regions or args.list_regions or config.region_service_key:
            with psycopg.connect(config.db_url, autocommit=True, connect_timeout=10) as conn:
                init_schema(conn)
                if not args.list_regions:
                    sync_codes(conn, config.region_service_key)
                if args.sync_regions:
                    return 0
                if args.list_regions:
                    for code, name in conn.execute('SELECT lawd_cd, address_name FROM budongsan.trade_region ORDER BY lawd_cd'):
                        print(f'{code}\t{name}')
                    return 0
                if config.region_names:
                    config = replace(config, regions=select_regions(conn, config.region_names))
                    log.info('Resolved selected names to %s trade regions', len(config.regions))
        return 0 if collect(config, months) else 1
    except Exception as exc:
        log.error('%s', str(exc) if isinstance(exc, (CollectionError, RegionCodeError)) else type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
