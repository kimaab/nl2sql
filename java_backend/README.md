# java_backend

`backend/` 의 파이썬(FastAPI) API 를 Spring Boot 로 옮긴 것이다. 경로, 요청과 응답 모양, 메타데이터 DB 는 파이썬과 같다.
프론트엔드는 그대로 붙는다 (기본 포트 8000).

## 실행

- Java 25, Maven 3.9
- 설정은 `../backend/.env`, `../.env`(프로젝트 루트), `./.env` 순으로 읽고 뒤의 파일이 앞의 값을 덮는다. 환경변수가 가장 먼저다.
  PyCharm 의 파이썬 실행 설정이 루트 `.env` 를 넣으므로, 이 순서면 IDE 에서 띄운 파이썬과 같은 LLM 을 부른다.

```
mvn package -DskipTests
java -jar target/nl2sql-backend-0.1.0.jar            # 8000 번
PORT=8001 java -jar target/nl2sql-backend-0.1.0.jar  # 파이썬과 함께 띄울 때
```

기동할 때 `src/main/resources/schema.sql` 을 돌린다 (`backend/schema.sql` 과 같은 내용이다. 스키마를 바꾸면 둘 다 고친다).

## 설정

| 이름 | 뜻 |
|---|---|
| DB_URL | 메타데이터 DB. `postgresql://user:pass@host:port/db` 나 `jdbc:postgresql://…` |
| NL2SQL_SECRET_KEY | 대상 DB 비밀번호 암호화 키. 파이썬과 같은 형식이라 서로 푼다 |
| LLM_BASE_URL, LLM_API_KEY, LLM_MODEL | OpenAI 호환 LLM |
| LLM_PROVIDER | `spring-ai` (기본) 또는 `langchain4j`. 같은 엔드포인트를 부른다 |
| AUTO_SYNC_MINUTES | 자동 동기화 주기 (분, 0 이면 끈다) |
| SQL_LOG | 1 이면 실행한 SQL 을 로그에 남긴다 |
| SUPERSET_URL, SUPERSET_USERNAME, SUPERSET_PASSWORD, SUPERSET_CA_BUNDLE | Superset 연동 |

## 파이썬과 대응

| 파이썬 | Java |
|---|---|
| app.py, *_routes.py | web/*Controller |
| models.py | model/*Models |
| db.py, secret.py, catalog.py | db/MetaDb, Secret, Catalog, TargetDb |
| compiler.py | compiler/Compiler, Dialect |
| sql_ast.py (sqlglot) | sqlast/SqlAst (JSqlParser) |
| llm.py, selector.py 공통부 | llm/LlmFactory, SpringAiLlm, LangChain4jLlm, LlmCalls |
| 그 밖의 모듈 | service/*Service |

## 대조 도구

`src/test/java/com/nl2sql/CrossCheck.java` 는 파이썬이 기록한 `parse_sql` · `compile` 호출을 같은 입력으로 돌려 결과를 낸다.
파이썬 결과와 비교해 컴파일러와 SQL → AST 변환이 같은지 본다.
