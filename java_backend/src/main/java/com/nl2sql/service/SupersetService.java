package com.nl2sql.service;

import java.io.InputStream;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.cert.Certificate;
import java.security.cert.CertificateFactory;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import com.nl2sql.common.ApiException;
import com.nl2sql.common.Dyn;
import com.nl2sql.common.Env;
import com.nl2sql.common.Json;
import com.nl2sql.common.Py;
import com.nl2sql.db.MetaDb;
import com.nl2sql.model.SystemModels.SystemInfo;

/**
 * Superset 연동: nl2sql 시스템을 Superset 의 DB 연결로 등록한다 (superset_link.py).
 *
 * 데이터는 옮기지 않는다 — Superset 은 차트를 그리거나 SQL Lab 에서 실행할 때마다 대상 DB 에 직접 쿼리한다.
 * 접속 정보(비밀번호 포함)는 nl2sql 이 가진 것을 그대로 보내, 운영자가 Superset 에 두 번 입력하지 않게 한다.
 * Superset 의 연결 id 는 meta_system.superset_database_id 에 남긴다 — 'Superset 에서 열기'(sqllabLink) 가 쓴다.
 *
 * 설정 (.env): SUPERSET_URL, SUPERSET_USERNAME, SUPERSET_PASSWORD, SUPERSET_CA_BUNDLE
 */
@Service
public class SupersetService {

    private static final Logger log = LoggerFactory.getLogger("nl2sql.superset");

    static final int TIMEOUT_S = 30;
    // Superset 이미지에 넣은 드라이버 (superset/Dockerfile). Oracle 은 없다.
    static final Map<String, String> SCHEMES = Map.of("postgresql", "postgresql+psycopg2", "mysql", "mysql+pymysql");
    // 차트가 읽을 최대 행 수, 선 차트에서 그릴 최대 선 수 (값이 큰 순)
    static final int ROW_LIMIT = 10000;
    static final int SERIES_LIMIT = 20;
    // 한 차트에 그릴 최대 숫자 컬럼 수
    static final int MAX_METRICS = 3;
    // Superset 의 GenericDataType: 0 숫자, 1 문자, 2 시각, 3 참거짓
    static final Map<Integer, String> GENERIC = Map.of(0, "number", 1, "text", 2, "time", 3, "text");

    private final MetaDb db;
    private final Env env;
    private final Connections connections;
    private final SystemService systems;

    public SupersetService(MetaDb db, Env env, Connections connections, SystemService systems) {
        this.db = db;
        this.env = env;
        this.connections = connections;
        this.systems = systems;
    }

    /**
     * 시스템을 Superset DB 연결로 등록한다. 이미 연결했으면 접속 정보를 지금 것으로 갱신한다.
     *
     * Superset 은 연결을 만들 때 대상 DB 에 접속해 본다 — 접속하지 못하면 등록되지 않고 그 이유를 돌려준다.
     */
    public SystemInfo linkDatabase(String systemId) {
        Map<String, Object> row = connections.row(systemId);
        String scheme = SCHEMES.get((String) row.get("driver"));
        if (scheme == null) {
            throw new ApiException(400, "Superset 에 " + row.get("driver") + " 드라이버가 없어 연결할 수 없습니다 (superset/Dockerfile)");
        }
        Map<String, Object> body = Dyn.of(
                "database_name", row.get("name"),
                "sqlalchemy_uri", uri(scheme, row),
                "expose_in_sqllab", true,
                "allow_dml", false,  // 조회만 한다
                "allow_run_async", false);  // Celery 없이 띄웠다 (superset/docker-compose.yml)
        Api api = new Api(env);
        Integer databaseId = find(api, row);
        if (databaseId == null) {
            databaseId = ((Number) api.call("POST", "/api/v1/database/", body, false).get("id")).intValue();
            log.info("superset database created: {} → #{}", row.get("name"), databaseId);
        } else {
            api.call("PUT", "/api/v1/database/" + databaseId, body, false);
            log.info("superset database updated: {} → #{}", row.get("name"), databaseId);
        }
        db.update("UPDATE meta_system SET superset_database_id = ?, updated_at = now() WHERE id = ?", databaseId, systemId);
        return systems.getSystem(systemId);
    }

    /**
     * SQL 을 Superset SQL Lab 에 열어 두는 링크. 실행은 사람이 SQL Lab 에서 누른다.
     *
     * SQL 을 URL 에 싣지 않고 Superset 의 permalink 로 저장한다 — 긴 SQL 도 잘리지 않는다.
     */
    public String sqllabLink(String systemId, String sql, String name) {
        Map<String, Object> row = linked(systemId);
        String title = name == null || name.isEmpty() ? (String) row.get("name") : name;
        Map<String, Object> state = Dyn.of("dbId", row.get("superset_database_id"), "sql", sql, "name",
                title.substring(0, Math.min(100, title.length())), "autorun", false);
        List<String> schemas = Dyn.strings(row.get("db_schemas"));
        if (!schemas.isEmpty()) {  // 기본 스키마. 다른 스키마의 테이블은 SQL 에 '스키마.테이블' 로 적혀 있다
            state.put("schema", schemas.get(0));
        }
        Api api = new Api(env);
        String key = String.valueOf(api.call("POST", "/api/v1/sqllab/permalink", state, false).get("key"));
        // 응답의 url 은 Superset 이 요청받은 호스트로 만든다 — 브라우저가 여는 주소는 SUPERSET_URL 로 맞춘다
        return api.base + "/sqllab/p/" + key;
    }

    /**
     * SQL 로 Superset 차트를 만들고 그 편집 화면(Explore) 링크를 돌려준다. → {url, viz_type, dataset_id, chart_id}
     *
     * 1. SQL 을 가상 데이터셋으로 등록한다. Superset 이 SQL 을 실행해 결과 컬럼과 타입을 읽는다.
     * 2. 컬럼 모양으로 차트 종류를 정해 차트를 저장한다 (chartParams).
     * 같은 SQL 이면 데이터셋과 차트를 새로 만들지 않고 전에 만든 것을 연다 — 질문을 다시 해도 쌓이지 않는다.
     */
    public Map<String, Object> createChart(String systemId, String sql, String name) {
        Map<String, Object> row = linked(systemId);
        String text = sql.strip();
        while (text.endsWith(";")) {  // 가상 데이터셋은 문장 하나만 받는다
            text = text.substring(0, text.length() - 1);
        }
        text = text.strip();
        String title = (name == null || name.isEmpty() ? (String) row.get("name") : name).strip();
        title = title.substring(0, Math.min(80, title.length()));
        Api api = new Api(env);
        long datasetId = dataset(api, row, text, title);
        List<Map<String, Object>> columns = Dyn.maps(Dyn.map(api.call("GET", "/api/v1/dataset/" + datasetId, null, false)
                .get("result")).get("columns"));
        Object[] chart = chartParams(datasetId, columns);
        long chartId = chart(api, datasetId, title, (String) chart[0], Dyn.map(chart[1]));
        return Dyn.of("url", api.base + "/explore/?slice_id=" + chartId, "viz_type", chart[0], "dataset_id", datasetId,
                "chart_id", chartId);
    }

    /**
     * 결과 컬럼 모양으로 차트 종류와 설정을 정한다 → [차트 종류, 설정].
     *
     * 시각 + 숫자 → 선 차트 (문자 컬럼이 있으면 그 값마다 선 하나)
     * 문자 + 숫자 → 막대 차트
     * 숫자 하나만 → 큰 숫자
     * 그 밖 → 표 (숫자 컬럼이 MAX_METRICS 보다 많을 때도)
     * 생성된 SQL 은 이미 집계한 결과라 차트는 다시 묶지 않는다 — 같은 칸에 한 행이면 SUM 이 그 값이다.
     */
    static Object[] chartParams(long datasetId, List<Map<String, Object>> columns) {
        List<String> times = new ArrayList<>();
        List<String> numbers = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        for (Map<String, Object> c : columns) {
            String kind = kind(c);
            String name = (String) c.get("column_name");
            if (kind.equals("time")) {
                times.add(name);
            } else if (kind.equals("number")) {
                numbers.add(name);
            } else {
                texts.add(name);
            }
        }
        Map<String, Object> base = Dyn.of("datasource", datasetId + "__table", "adhoc_filters", new ArrayList<>(), "row_limit", ROW_LIMIT);
        // 숫자 컬럼이 많으면 대개 원본 행 조회다 — 한 차트에 다 그리면 읽을 수 없어 표로 둔다
        boolean plottable = !numbers.isEmpty() && numbers.size() <= MAX_METRICS;

        if (!times.isEmpty() && plottable) {
            Map<String, Object> params = new LinkedHashMap<>(base);
            params.put("viz_type", "echarts_timeseries_line");
            params.put("x_axis", times.get(0));
            params.put("time_grain_sqla", null);  // 결과의 시각을 그대로 쓴다
            params.put("metrics", metrics(numbers));
            params.put("groupby", texts.subList(0, Math.min(1, texts.size())));
            // 선이 너무 많으면 첫 숫자 컬럼 합이 큰 SERIES_LIMIT 개만 그린다 (기준 지표가 없으면 제한이 먹지 않는다)
            params.put("limit", texts.isEmpty() ? null : SERIES_LIMIT);  // 화면의 Series limit
            params.put("timeseries_limit_metric", metrics(numbers).get(0));  // 화면의 Sort query by
            params.put("order_desc", true);
            params.put("show_legend", true);
            params.put("rich_tooltip", true);
            return new Object[] {"echarts_timeseries_line", params};
        }
        if (!texts.isEmpty() && plottable) {
            Map<String, Object> params = new LinkedHashMap<>(base);
            params.put("viz_type", "echarts_timeseries_bar");
            params.put("x_axis", texts.get(0));
            params.put("metrics", metrics(numbers));
            params.put("groupby", texts.subList(Math.min(1, texts.size()), Math.min(2, texts.size())));
            // 값이 큰 막대부터
            params.put("x_axis_sort", numbers.get(0));
            params.put("x_axis_sort_asc", false);
            params.put("show_legend", true);
            params.put("rich_tooltip", true);
            return new Object[] {"echarts_timeseries_bar", params};
        }
        if (numbers.size() == 1 && columns.size() == 1) {
            Map<String, Object> params = new LinkedHashMap<>(base);
            params.put("viz_type", "big_number_total");
            params.put("metric", metrics(numbers).get(0));
            return new Object[] {"big_number_total", params};
        }
        List<String> allColumns = new ArrayList<>();
        for (Map<String, Object> c : columns) {
            allColumns.add((String) c.get("column_name"));
        }
        Map<String, Object> params = new LinkedHashMap<>(base);
        params.put("viz_type", "table");
        params.put("query_mode", "raw");
        params.put("all_columns", allColumns);
        return new Object[] {"table", params};
    }

    static String kind(Map<String, Object> column) {
        if (Py.truthy(column.get("is_dttm"))) {
            return "time";
        }
        Object generic = column.get("type_generic");
        if (generic instanceof Number n && GENERIC.containsKey(n.intValue())) {
            return GENERIC.get(n.intValue());
        }
        String typeName = Py.text(column.get("type")).toUpperCase();
        for (String word : List.of("INT", "NUMERIC", "DECIMAL", "DOUBLE", "FLOAT", "REAL")) {
            if (typeName.contains(word)) {
                return "number";
            }
        }
        return "text";
    }

    static List<Object> metrics(List<String> numbers) {
        List<Object> out = new ArrayList<>();
        for (String name : numbers) {
            out.add(Dyn.of("expressionType", "SIMPLE", "column", Dyn.of("column_name", name), "aggregate", "SUM",
                    "label", name, "hasCustomLabel", true, "optionName", "metric_" + name));
        }
        return out;
    }

    /** SQL 의 가상 데이터셋 id. 이름에 SQL 해시를 넣어 같은 SQL 이면 전에 만든 것을 쓴다. */
    private long dataset(Api api, Map<String, Object> row, String sql, String title) {
        String tableName = "nl2sql " + title + " [" + sha1(sql).substring(0, 10) + "]";
        Map<String, Object> found = api.call("GET", "/api/v1/dataset/?q=" + encode(
                "(filters:!((col:table_name,opr:eq,value:" + rison(tableName) + ")),page_size:1)"), null, false);
        for (Map<String, Object> d : Dyn.maps(found.get("result"))) {
            return ((Number) d.get("id")).longValue();
        }
        Map<String, Object> body = Dyn.of("database", row.get("superset_database_id"), "table_name", tableName, "sql", sql);
        List<String> schemas = Dyn.strings(row.get("db_schemas"));
        if (!schemas.isEmpty()) {
            body.put("schema", schemas.get(0));
        }
        long datasetId = ((Number) api.call("POST", "/api/v1/dataset/", body, false).get("id")).longValue();
        log.info("superset dataset created: {} → #{}", tableName, datasetId);
        return datasetId;
    }

    /** 데이터셋의 차트 id. 이 데이터셋으로 만든 차트가 있으면 그것을 연다 (사람이 고친 설정을 덮지 않는다). */
    private long chart(Api api, long datasetId, String title, String vizType, Map<String, Object> params) {
        Map<String, Object> found = api.call("GET", "/api/v1/chart/?q=" + encode(
                "(filters:!((col:datasource_id,opr:eq,value:" + datasetId + ")),page_size:1)"), null, false);
        for (Map<String, Object> c : Dyn.maps(found.get("result"))) {
            return ((Number) c.get("id")).longValue();
        }
        Map<String, Object> body = Dyn.of("slice_name", title, "viz_type", vizType, "datasource_id", datasetId,
                "datasource_type", "table", "params", Json.dumps(params));
        long chartId = ((Number) api.call("POST", "/api/v1/chart/", body, false).get("id")).longValue();
        log.info("superset chart created: {} ({}) → #{}", title, vizType, chartId);
        return chartId;
    }

    /** rison 문자열 (Superset 목록 API 의 q 인자) */
    static String rison(String text) {
        return "'" + text.replace("!", "!!").replace("'", "!'") + "'";
    }

    /** Superset 에 연결한 시스템 행. 연결하지 않았으면 400. */
    private Map<String, Object> linked(String systemId) {
        Map<String, Object> row = db.one("SELECT name, db_schemas, superset_database_id FROM meta_system WHERE id = ?", systemId);
        if (row == null) {
            throw new ApiException(404, "시스템을 찾을 수 없습니다");
        }
        if (!Py.truthy(row.get("superset_database_id"))) {
            throw new ApiException(400, row.get("name") + " 은 아직 Superset 에 연결하지 않았습니다 (시스템 목록의 Superset 연결)");
        }
        return row;
    }

    static String uri(String scheme, Map<String, Object> row) {
        String user = quote(Py.text(row.get("username")));
        String password = quote(Py.text(row.get("password")));
        String auth = user.isEmpty() ? "" : user + ":" + password + "@";
        return scheme + "://" + auth + row.get("host") + ":" + row.get("port") + "/" + row.get("db_name");
    }

    /** 이 시스템의 Superset 연결 id. 남겨 둔 id 가 Superset 에 없으면 이름으로 찾는다 (손으로 만든 연결도 이어 쓴다). */
    private static Integer find(Api api, Map<String, Object> row) {
        Object saved = row.get("superset_database_id");
        if (saved instanceof Number n && api.call("GET", "/api/v1/database/" + n.intValue(), null, true) != null) {
            return n.intValue();
        }
        Map<String, Object> found = api.call("GET", "/api/v1/database/?q=" + encode("(page_size:100)"), null, false);
        for (Map<String, Object> d : Dyn.maps(found.get("result"))) {
            if (row.get("name").equals(d.get("database_name"))) {
                return ((Number) d.get("id")).intValue();
            }
        }
        return null;
    }

    /** urllib.parse.quote(text, safe="") */
    static String quote(String text) {
        return URLEncoder.encode(text, StandardCharsets.UTF_8).replace("+", "%20").replace("*", "%2A").replace("%7E", "~");
    }

    private static String encode(String text) {
        return URLEncoder.encode(text, StandardCharsets.UTF_8);
    }

    private static String sha1(String text) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-1").digest(text.getBytes(StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException error) {
            throw new IllegalStateException(error);
        }
    }

    /** Superset REST API. 로그인(JWT)과 CSRF 토큰을 한 세션에 들고 부른다 — 쓰기 요청은 CSRF 토큰이 없으면 거절된다. */
    static final class Api {
        final String base;
        private final HttpClient client;
        private final Map<String, String> headers = new LinkedHashMap<>();

        Api(Env env) {
            String url = env.get("SUPERSET_URL").strip();
            while (url.endsWith("/")) {
                url = url.substring(0, url.length() - 1);
            }
            this.base = url;
            String user = env.get("SUPERSET_USERNAME").strip();
            String password = env.get("SUPERSET_PASSWORD");
            if (base.isEmpty() || user.isEmpty() || password.isEmpty()) {
                throw new ApiException(400, "Superset 연동 설정이 없습니다 (.env 의 SUPERSET_URL, SUPERSET_USERNAME, SUPERSET_PASSWORD)");
            }
            HttpClient.Builder builder = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(TIMEOUT_S))
                    .cookieHandler(new java.net.CookieManager());
            // 자체 서명 인증서로 띄운 Superset 은 그 CA 로 검증한다 (superset/certs/ca.crt)
            String caBundle = env.get("SUPERSET_CA_BUNDLE").strip();
            if (!caBundle.isEmpty()) {
                builder.sslContext(sslContext(caBundle));
            }
            this.client = builder.build();
            Map<String, Object> login = call("POST", "/api/v1/security/login",
                    Dyn.of("username", user, "password", password, "provider", "db", "refresh", false), false);
            headers.put("Authorization", "Bearer " + login.get("access_token"));
            String csrf = String.valueOf(call("GET", "/api/v1/security/csrf_token/", null, false).get("result"));
            headers.put("X-CSRFToken", csrf);
            headers.put("Referer", base);
        }

        /** → 응답 JSON. missingOk 이면 404 일 때 null. */
        Map<String, Object> call(String method, String path, Map<String, Object> body, boolean missingOk) {
            HttpRequest.Builder request = HttpRequest.newBuilder(URI.create(base + path)).timeout(Duration.ofSeconds(TIMEOUT_S));
            for (Map.Entry<String, String> h : headers.entrySet()) {
                request.header(h.getKey(), h.getValue());
            }
            if (body != null) {
                request.header("Content-Type", "application/json");
                request.method(method, HttpRequest.BodyPublishers.ofString(Json.dumps(body)));
            } else {
                request.method(method, HttpRequest.BodyPublishers.noBody());
            }
            HttpResponse<String> res;
            try {
                res = client.send(request.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            } catch (java.io.IOException error) {
                throw new ApiException(502, "Superset(" + base + ") 에 접속하지 못했습니다: " + Py.firstLine(error));
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                throw new ApiException(502, "Superset(" + base + ") 요청이 중단됐습니다");
            }
            if (missingOk && res.statusCode() == 404) {
                return null;
            }
            if (res.statusCode() >= 400) {
                throw new ApiException(502, "Superset 이 요청을 거절했습니다 (" + method + " " + path.split("\\?")[0] + ", "
                        + res.statusCode() + "): " + reason(res.body()));
            }
            String text = res.body();
            if (text == null || text.isEmpty()) {
                return new LinkedHashMap<>();
            }
            Map<String, Object> parsed = Dyn.map(Json.parse(text));
            return parsed == null ? new LinkedHashMap<>() : parsed;
        }

        private static SSLContext sslContext(String caBundle) {
            try (InputStream in = Files.newInputStream(Path.of(caBundle))) {
                KeyStore store = KeyStore.getInstance(KeyStore.getDefaultType());
                store.load(null, null);
                int i = 0;
                for (Certificate cert : CertificateFactory.getInstance("X.509").generateCertificates(in)) {
                    store.setCertificateEntry("ca" + i, cert);
                    i += 1;
                }
                TrustManagerFactory trust = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
                trust.init(store);
                SSLContext context = SSLContext.getInstance("TLS");
                context.init(null, trust.getTrustManagers(), null);
                return context;
            } catch (Exception error) {
                throw new ApiException(400, "SUPERSET_CA_BUNDLE 을 읽지 못했습니다 (" + caBundle + "): " + Py.firstLine(error));
            }
        }

        /** Superset 오류 응답에서 사람이 읽을 이유 한 줄 */
        private static String reason(String text) {
            Map<String, Object> body;
            try {
                body = Dyn.map(Json.parse(text));
            } catch (RuntimeException error) {
                return text == null ? "" : text.substring(0, Math.min(200, text.length()));
            }
            if (body == null) {
                return text.substring(0, Math.min(200, text.length()));
            }
            List<Object> errors = Dyn.items(body.get("errors"));
            String out;
            if (!errors.isEmpty()) {
                Map<String, Object> first = Dyn.map(errors.get(0));
                out = first != null && Py.truthy(first.get("message")) ? String.valueOf(first.get("message")) : Json.dumps(errors.get(0));
            } else {
                out = Py.truthy(body.get("message")) ? Json.dumps(body.get("message")).replaceAll("^\"|\"$", "") : Json.dumps(body);
            }
            return out.substring(0, Math.min(300, out.length()));
        }
    }
}
