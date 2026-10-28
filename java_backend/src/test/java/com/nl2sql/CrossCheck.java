package com.nl2sql;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.nl2sql.common.Dyn;
import com.nl2sql.common.Json;
import com.nl2sql.compiler.CalcColumnError;
import com.nl2sql.compiler.Compiler;
import com.nl2sql.sqlast.SqlAst;

/**
 * 파이썬 결과와 대조하는 도구. 입력: 파이썬이 기록한 호출 목록 (kind = parse | compile | metric).
 * 출력: 같은 입력을 Java 로 돌린 결과. 비교는 파이썬 쪽 스크립트가 한다.
 *
 *   java CrossCheck 입력.json 출력.json
 */
public class CrossCheck {

    public static void main(String[] args) throws Exception {
        Object input = Json.parse(Files.readString(Path.of(args[0])));
        List<Object> out = new ArrayList<>();
        for (Map<String, Object> rec : Dyn.maps(input)) {
            Map<String, Object> result = new LinkedHashMap<>();
            String kind = (String) rec.get("kind");
            if (kind.equals("parse")) {
                try {
                    SqlAst.Parsed parsed = SqlAst.parseSql((String) rec.get("sql"), Dyn.map(rec.get("contract")));
                    result.put("ast", parsed.ast());
                    result.put("warnings", parsed.warnings());
                    result.put("tables", Compiler.astTables(parsed.ast()));
                } catch (RuntimeException error) {
                    result.put("error", error.getMessage());
                }
            } else if (kind.equals("compile")) {
                try {
                    List<Map<String, Object>> metrics = rec.get("metrics") == null ? null : Dyn.maps(rec.get("metrics"));
                    Compiler compiler = new Compiler(Dyn.map(rec.get("contract")), metrics,
                            rec.get("utc_now") == null ? null : Compiler.parseDatetime(rec.get("utc_now")));
                    result.put("sql", compiler.compile(rec.get("ast")));
                } catch (CalcColumnError error) {
                    result.put("error", error.getMessage());
                    result.put("error_type", "CalcColumnError");
                } catch (RuntimeException error) {
                    result.put("error", error.getMessage());
                    result.put("error_type", error.getClass().getSimpleName());
                }
            } else if (kind.equals("positional")) {
                result.put("positional", SqlAst.positionalRefs((String) rec.get("sql")));
            }
            out.add(result);
        }
        Files.writeString(Path.of(args[1]), Json.dumps(out));
    }
}
