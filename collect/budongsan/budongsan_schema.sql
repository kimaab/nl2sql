CREATE SCHEMA IF NOT EXISTS budongsan;

CREATE TABLE IF NOT EXISTS budongsan.region_code (
    region_cd varchar(10) PRIMARY KEY CHECK (region_cd ~ '^[0-9]{10}$'),
    address_name text NOT NULL,
    parent_code varchar(10),
    is_current boolean NOT NULL DEFAULT true,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    raw_data jsonb NOT NULL
);
COMMENT ON TABLE budongsan.region_code IS '행정안전부 법정동코드. 최신 전체 API 응답에 없는 코드는 삭제하지 않고 is_current=false';
COMMENT ON COLUMN budongsan.region_code.is_current IS '최신 API 응답 포함 여부. 법적 폐지 확정을 뜻하지 않음';
CREATE TABLE IF NOT EXISTS budongsan.region_sync_run (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    row_count integer NOT NULL DEFAULT 0,
    error text
);
-- Derive query prefixes from actual town/dong entries, excluding province and
-- parent-city headings that have no directly attached town/dong of their own.
CREATE OR REPLACE VIEW budongsan.trade_region AS
SELECT DISTINCT left(d.region_cd, 5)::varchar(5) AS lawd_cd, p.address_name
FROM budongsan.region_code d
JOIN budongsan.region_code p ON p.region_cd=left(d.region_cd,5)||'00000'
WHERE d.is_current AND p.is_current
  AND substring(d.region_cd,3,3)<>'000' AND substring(d.region_cd,6,3)<>'000'
  AND right(d.region_cd,2)='00';
COMMENT ON VIEW budongsan.trade_region IS '현재 읍면동 코드에서 추출한 실거래가 API 조회용 5자리 시군구 코드';

CREATE TABLE IF NOT EXISTS budongsan.collection_run (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    regions text[] NOT NULL,
    months text[] NOT NULL,
    partitions_saved integer NOT NULL DEFAULT 0,
    rows_saved integer NOT NULL DEFAULT 0,
    error text
);

-- The API has no transaction ID. Replace a complete region/month atomically;
-- row_no is only a position in that snapshot, never a permanent transaction ID.
CREATE TABLE IF NOT EXISTS budongsan.apartment_trade (
    lawd_cd varchar(5) NOT NULL,
    deal_ym varchar(6) NOT NULL,
    row_no integer NOT NULL,
    apartment_name text NOT NULL,
    legal_dong text,
    jibun text,
    apartment_dong text,
    exclusive_area_m2 numeric NOT NULL CHECK (exclusive_area_m2 > 0),
    floor integer,
    build_year integer,
    deal_date date NOT NULL,
    deal_amount_manwon bigint NOT NULL CHECK (deal_amount_manwon > 0),
    is_cancelled boolean NOT NULL,
    cancellation_date date,
    registration_date date,
    dealing_type text,
    estate_agent_region text,
    seller_type text,
    buyer_type text,
    land_leasehold_type text,
    raw_data jsonb NOT NULL,
    collected_at timestamptz NOT NULL DEFAULT now(),
    collection_run_id bigint NOT NULL REFERENCES budongsan.collection_run(id),
    PRIMARY KEY (lawd_cd, deal_ym, row_no)
);
CREATE INDEX IF NOT EXISTS apartment_trade_date_idx
    ON budongsan.apartment_trade (deal_date);
CREATE INDEX IF NOT EXISTS apartment_trade_name_idx
    ON budongsan.apartment_trade (lawd_cd, apartment_name);
COMMENT ON TABLE budongsan.apartment_trade IS '국토교통부 아파트 매매 실거래가. 지역·계약월별 최신 전체 응답';
COMMENT ON COLUMN budongsan.apartment_trade.lawd_cd IS '지역코드 (sggCd). 법정동코드 앞 5자리';
COMMENT ON COLUMN budongsan.apartment_trade.deal_ym IS '계약년월. 계약년도(dealYear)와 계약월(dealMonth)을 결합한 YYYYMM 형식';
COMMENT ON COLUMN budongsan.apartment_trade.row_no IS '월별 응답 내 순번. 재수집 시 변경될 수 있으며 거래 고유 ID가 아님';
COMMENT ON COLUMN budongsan.apartment_trade.apartment_name IS '단지명 (aptNm)';
COMMENT ON COLUMN budongsan.apartment_trade.legal_dong IS '법정동 (umdNm)';
COMMENT ON COLUMN budongsan.apartment_trade.jibun IS '지번 (jibun)';
COMMENT ON COLUMN budongsan.apartment_trade.apartment_dong IS '아파트 동명 (aptDong)';
COMMENT ON COLUMN budongsan.apartment_trade.exclusive_area_m2 IS '전용면적 (excluUseAr). 단위: 제곱미터(㎡)';
COMMENT ON COLUMN budongsan.apartment_trade.floor IS '층 (floor)';
COMMENT ON COLUMN budongsan.apartment_trade.build_year IS '건축년도 (buildYear)';
COMMENT ON COLUMN budongsan.apartment_trade.deal_date IS '계약일자. 계약년도(dealYear), 계약월(dealMonth), 계약일(dealDay)을 결합한 날짜';
COMMENT ON COLUMN budongsan.apartment_trade.deal_amount_manwon IS '거래금액 (dealAmount). 단위: 만원. 원 단위 환산 시 10000을 곱함';
COMMENT ON COLUMN budongsan.apartment_trade.is_cancelled IS '해제여부 (cdealType). true: 해제된 거래, false: 해제되지 않은 거래';
COMMENT ON COLUMN budongsan.apartment_trade.cancellation_date IS '해제사유발생일 (cdealDay)';
COMMENT ON COLUMN budongsan.apartment_trade.registration_date IS '등기일자 (rgstDate)';
COMMENT ON COLUMN budongsan.apartment_trade.dealing_type IS '거래유형 (dealingGbn). 중개 및 직거래 여부';
COMMENT ON COLUMN budongsan.apartment_trade.estate_agent_region IS '중개사소재지 (estateAgentSggNm). 시군구 단위';
COMMENT ON COLUMN budongsan.apartment_trade.seller_type IS '매도자 거래주체정보 (slerGbn). 개인/법인/공공기관/기타';
COMMENT ON COLUMN budongsan.apartment_trade.buyer_type IS '매수자 거래주체정보 (buyerGbn). 개인/법인/공공기관/기타';
COMMENT ON COLUMN budongsan.apartment_trade.land_leasehold_type IS '토지임대부 아파트 여부 (landLeaseholdGbn)';
COMMENT ON COLUMN budongsan.apartment_trade.raw_data IS '개별 거래 항목(item)의 API 원본 데이터. 항목명과 문자열 값을 JSON으로 보관';
COMMENT ON COLUMN budongsan.apartment_trade.collected_at IS '수집일시. 해당 지역·계약월의 거래 데이터를 DB에 저장한 시각';
COMMENT ON COLUMN budongsan.apartment_trade.collection_run_id IS '수집 실행 ID. budongsan.collection_run의 수집 이력 참조';
