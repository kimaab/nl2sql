# nl2sql Studio

자연어 질문을 SQL 쿼리로 변환하는 웹 애플리케이션입니다.

## 기능

- **여러 데이터베이스 지원**: MySQL, PostgreSQL, Oracle
- **스키마 자동 동기화**: 대상 DB에서 스키마를 읽어 메타데이터 DB에 저장
- **자연어 → SQL**: LLM을 이용한 자동 변환
- **업무 지표 관리**: 사용자 정의 지표로 더 정확한 쿼리 생성
- **안전한 SQL 조립**: 모델이 직접 SQL을 생성하지 않고, 검증된 컴파일러를 통해 생성

## 설치

### 요구사항
- Python 3.12+
- Node.js 18+
- PostgreSQL (메타데이터 DB로 사용)

### 백엔드 설정

```bash
# 의존성 설치
pip install -e .

# 환경변수 설정
cp .env.example .env
# .env 파일 수정: DB_URL, LLM 정보 등

# 백엔드 실행
cd backend
python -m uvicorn app:app --reload
```

### 프론트엔드 설정

```bash
cd frontend
npm install
npm run dev
```

## 구조

```
nl2sql-studio/
├── backend/
│   ├── app.py              # FastAPI 애플리케이션
│   ├── schema.sql          # 메타데이터 DB 스키마
│   ├── models.py           # Pydantic 모델
│   ├── db.py               # DB 커넥션 풀
│   ├── catalog.py          # 드라이버별 카탈로그 읽기
│   ├── datasources.py      # 데이터소스 CRUD + 동기화
│   ├── metrics.py          # 업무 지표 CRUD
│   ├── contract.py         # 계약서 로드
│   ├── pruner.py           # TF-IDF 프루닝
│   ├── compiler.py         # AST 검증 + SQL 조립
│   ├── tool.py             # compile_sql 도구
│   ├── graph.py            # LangGraph 에이전트 루프
│   └── logging_config.py   # 로깅 설정
└── frontend/
    └── src/
        ├── App.tsx         # 메인 앱
        ├── DatasourcePage.tsx   # 데이터소스 관리
        ├── AskPage.tsx     # SQL 생성
        └── api.ts          # API 클라이언트
```

## 환경변수

| 변수 | 설명 |
|------|------|
| `DB_URL` | 메타데이터 DB 연결 문자열 (PostgreSQL) |
| `LLM_BASE_URL` | LLM API 엔드포인트 |
| `LLM_API_KEY` | LLM API 키 |
| `LLM_MODEL` | 모델 이름 (예: gpt-3.5-turbo) |

## API 엔드포인트

### 데이터소스
- `GET /api/datasources` - 목록
- `POST /api/datasources` - 등록
- `GET /api/datasources/{id}` - 조회
- `PUT /api/datasources/{id}` - 수정
- `DELETE /api/datasources/{id}` - 삭제
- `POST /api/datasources/{id}/sync` - 스키마 동기화
- `GET /api/datasources/{id}/schema` - 저장된 스키마 조회

### 지표
- `GET /api/datasources/{id}/metrics` - 지표 목록
- `POST /api/datasources/{id}/metrics` - 지표 등록
- `DELETE /api/datasources/{id}/metrics/{metric_id}` - 지표 삭제

### 질문
- `POST /api/ask` - 질문에서 SQL 생성

## 주요 설계 원칙

1. **SQL 문자열은 모델이 생성하지 않음** - JSON AST만 생성하고 컴파일러가 검증 후 SQL 조립
2. **등록과 동기화는 분리** - 실패의 성격이 다르므로 별도 엔드포인트
3. **동기화는 통째로 교체** - 병합하지 않음 (테이블 삭제를 반영하기 위해)
4. **요청 간 격리** - 다음 질문은 이전 질문을 모름
5. **지표의 고정 필터는 컴파일러가 강제** - 프롬프트만으로 신뢰하지 않음

## 라이선스

MIT
