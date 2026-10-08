# 아파트 매매 실거래가 수집

공식 API: [국토교통부_아파트 매매 실거래가 자료](https://www.data.go.kr/data/15126469/openapi.do).
해당 API의 활용신청이 승인된 인증키가 필요합니다. 상세 자료 API의 키 권한과 구분하세요.
요청은 `LAWD_CD`(법정동코드 앞 5자리), `DEAL_YMD`(계약년월) 단위이며 응답은 XML입니다.
수집 대상은 신고된 아파트 매매 내역입니다. 전국 모든 아파트의 단지 대장은 아닙니다.

## 설정 및 실행

프로젝트 루트에서 `uv sync` 후 **기존 `backend/.env`에** 아래 설정을 추가합니다.
환경변수 > backend/.env > 루트 .env 순서로 적용합니다. 기존 파일을 덮어쓰지 마세요.

```dotenv
MOLIT_SERVICE_KEY=공공데이터포털_인증키
BUDONGSAN_LAWD_CODES=11110,11680
BUDONGSAN_LOOKBACK_MONTHS=3
# 생략하면 기존 DB_URL 사용
BUDONGSAN_DB_URL=
```

지역 코드는 예시(종로구, 강남구)입니다. 실제 수집할 시군구를 명시하세요.
인증키는 Decoding/Encoding 모두 지원하며 요청 시 한 번만 URL 인코딩합니다.
키와 DB 비밀번호는 로그나 스케줄러 인수에 기록하지 않습니다.

```powershell
.\.venv\Scripts\python.exe collect\budongsan\budongsan.py --init-db
.\.venv\Scripts\python.exe collect\budongsan\budongsan.py --check-config
# 당월 포함 최근 3개월 수집
.\.venv\Scripts\python.exe collect\budongsan\budongsan.py
# 과거 자료 수집 (양끝 월 포함)
.\.venv\Scripts\python.exe collect\budongsan\budongsan.py --start-month 202510 --end-month 202609
```

`--check-config`는 형식만 검사합니다. API 권한, 지역 코드의 실제 존재 여부, DB 연결은 실제 실행에서 확인됩니다.
과거 자료 범위는 자동으로 추정하지 않습니다. 과거 자료를 최초 적재하려면 월 범위를 지정하세요.

## 법정동코드 자동 연동

[행정안전부_행정표준코드_법정동코드](https://www.data.go.kr/data/15077871/openapi.do)의 활용신청이 승인된 키를 `MOIS_SERVICE_KEY`에 설정합니다.
요청 주소는 `https://apis.data.go.kr/1741000/StanReginCd/getStanReginCdList`입니다.
아파트 거래 API 키와 같아도 두 서비스 각각 이용 권한이 필요합니다.

```dotenv
MOIS_SERVICE_KEY=법정동코드_API_승인키
# 공식 시도/시군구 명칭. 쉼표로 여러 지역 선택 가능. 전국은 명시적으로 '전국'
BUDONGSAN_REGIONS=서울특별시
BUDONGSAN_LAWD_CODES=
```

```powershell
# 거래는 수집하지 않고 전국 법정동코드만 갱신
.\.venv\Scripts\python.exe collect\budongsan\budongsan.py --sync-regions
# DB에 저장된 실거래가 조회용 시군구 목록
.\.venv\Scripts\python.exe collect\budongsan\budongsan.py --list-regions
```

`budongsan.region_code`에 10자리 코드·지역명·상위 코드·원본을 보관하고,
`budongsan.trade_region` 뷰에서 조회용 5자리 코드와 지역명을 제공합니다.
`budongsan.region_sync_run`에는 코드 갱신 이력이 기록됩니다.
전체 페이지 검증 후 한 트랜잭션으로 갱신하며, 응답에서 빠진 과거 코드는 삭제하지 않고 `is_current=false`로 남깁니다.
이는 최신 API 응답 포함 여부이며, 법적 폐지 여부를 확정하는 값은 아닙니다.

지역명 설정 예시는 `서울특별시`, `경기도`, `경기도 수원시`, `경기도 수원시 장안구`, `세종특별자치시`, `전국`입니다.
읍면동 단위 필터는 지원하지 않습니다. 실거래가 API의 조회 범위가 시군구 단위이기 때문입니다.
구가 있는 시를 선택하면 실제 읍면동이 소속된 각 구의 코드를 사용하고, 부모 시 코드를 중복 조회하지 않습니다.

`MOIS_SERVICE_KEY`가 설정된 경우 매일 기존 06:00 작업에서 코드 갱신 → 지역명 해석 → 거래 수집 순서로 실행됩니다.
코드 갱신 실패 시 기존 목록은 보존하고 해당 실행은 오류 종료합니다. 예약 작업의 재시도 설정을 따릅니다.
지역명이 미설정이면 전국으로 자동 확대하지 않습니다. `BUDONGSAN_REGIONS` 또는 `BUDONGSAN_LAWD_CODES`를 먼저 지정하세요.
지역명 선택은 현재 코드 목록 기준입니다. 행정구역 개편 전 과거 거래를 수집할 때에는 당시 5자리 코드를 직접 지정해야 할 수 있습니다.

### 전국 순차 수집

`BUDONGSAN_REGIONS=전국`, `BUDONGSAN_LAWD_CODES=`로 설정하면 전체 시군구를 5자리 코드 오름차순으로 수집합니다.
각 시군구에서 조회 대상 월을 오래된 월부터 순서대로 완료한 후 다음 시군구로 넘어갑니다.
API 요청은 병렬 실행하지 않으며, 요청 전 0.2초 대기와 오류 시 지수 증가 대기를 적용합니다.
기본 최근 3개월 설정에서는 시군구 수 × 3개의 지역·월을 처리합니다(추가 페이지와 재시도는 별도 호출).
진행률은 `logs/budongsan.log`의 `Progress 현재/전체`에서 확인할 수 있습니다.
수집 실패 지역·월은 기록하고 다음 대상으로 진행하며, 재실행 시 전체 범위를 다시 갱신합니다.

## 매일 오전 6시 실행

Windows 작업 스케줄러에 한 번 등록합니다. 웹 서버 실행 여부와 독립적입니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File collect\budongsan\register_budongsan_task.ps1
Get-ScheduledTaskInfo -TaskName nl2sql-budongsan-daily
# 즉시 실행
Start-ScheduledTask -TaskName nl2sql-budongsan-daily
# 중지하려면
Disable-ScheduledTask -TaskName nl2sql-budongsan-daily
```

한국 시간대(`Korea Standard Time`)에서 06:00에 실행되며, 실행을 놓치면 실행 가능한 시점에 시작합니다.
오류 종료 시 15분 간격으로 최대 3회 재시도합니다. 기본 등록은 현재 사용자의 로그인 세션을 사용합니다.
**PC가 켜져 있고 사용자가 로그인되어 있어야 합니다**(화면 잠금은 가능). 절전 해제는 OS/하드웨어 설정에 따라 제한될 수 있습니다.
로그아웃 상태에서도 실행해야 하면 작업 스케줄러에서 ‘사용자의 로그온 여부에 관계없이 실행’을 선택하고 Windows 자격 증명을 직접 설정하거나, 상시 서버에서 운영하세요.
인증키/지역 설정을 나중에 넣을 경우 `-AllowIncompleteConfig`로 등록할 수 있으나, 설정 전에는 실행이 오류 종료됩니다.
기존 같은 이름의 작업은 자동으로 덮어쓰지 않습니다.

Linux에서는 KST로 설정된 상시 서버의 cron에 아래와 같이 등록할 수 있습니다(경로 교체).

```cron
0 6 * * * cd /path/to/nl2sql && /path/to/nl2sql/.venv/bin/python collect/budongsan/budongsan.py >> logs/budongsan-cron.log 2>&1
```

## 저장과 오류 처리

- `budongsan.apartment_trade`: 지역·계약월별 최신 응답. 아파트명, 법정동, 지번, 동, 전용면적(㎡), 층, 건축년도, 계약일, 거래금액(**만원**), 해제 여부/일자, 등기일, 거래유형, 거래주체, 원본 JSON을 보관합니다.
- `budongsan.collection_run`: 수집 시작/종료 시간, 성공/실패, 대상 지역/월, 저장 건수, 오류를 기록합니다. 파일 로그는 `logs/budongsan.log`에 30일 보관합니다.
- 페이지를 모두 받은 뒤 검증하고 지역·월별로 트랜잭션 교체합니다. 일부 페이지 실패나 저장 실패 시 해당 지역·월의 기존 자료를 유지합니다. 성공한 다른 지역·월은 저장되며 전체 실행은 실패 상태로 표시됩니다.
- 정상적인 0건 응답은 해당 지역·월의 기존 자료를 비웁니다. API 페이지 수/건수 불일치는 실패로 처리합니다.
- 공개 API에는 거래 고유번호가 없으므로 임의의 가격·면적·층 조합으로 중복 제거하지 않습니다. 조건이 같은 실제 복수 거래를 보존합니다. `row_no`는 응답 내 순번이며 영구 거래 ID가 아닙니다.
- 같은 범위를 다시 수집해도 건수가 누적되지 않습니다. 해제나 정정은 해당 지역·월의 최신 응답으로 반영합니다. 변경 전 거래별 이력은 보관하지 않습니다.
- PostgreSQL advisory lock으로 수동/예약 수집의 동시 실행을 막습니다. 비정상 종료된 실행은 다음 수집 시 실패로 표시합니다.
- 네트워크/서버/일시 제한 오류는 최대 4회 재시도합니다. 인증/일일 한도 오류 등은 실행 이력에 남깁니다.

매일 최근 3개월만 갱신하므로 **그보다 오래된 거래의 뒤늦은 정정·해제는 자동 반영되지 않습니다**.
필요에 따라 조회 기간을 늘리거나 과거 범위를 주기적으로 재수집하세요. 많은 지역·월을 요청할 때에는 API 이용 한도를 확인하세요.

```sql
-- 해제되지 않은 거래 (가격 단위: 만원)
SELECT apartment_name, legal_dong, deal_date, exclusive_area_m2,
       floor, deal_amount_manwon
FROM budongsan.apartment_trade
WHERE NOT is_cancelled
ORDER BY deal_date DESC;

SELECT * FROM budongsan.collection_run ORDER BY id DESC LIMIT 20;
```

nl2sql Studio에서 조회하려면 PostgreSQL 시스템을 등록할 때 스키마를 `budongsan`으로 지정한 뒤 동기화하세요.
