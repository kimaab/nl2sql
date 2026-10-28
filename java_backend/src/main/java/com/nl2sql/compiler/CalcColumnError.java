package com.nl2sql.compiler;

/** [계산 필요] 컬럼을 지표에 없는 방식으로 계산했다. table, column: 그 컬럼 */
public class CalcColumnError extends AstError {

    private final String table;
    private final String column;

    public CalcColumnError(String message, String table, String column) {
        super(message);
        this.table = table;
        this.column = column;
    }

    public String table() {
        return table;
    }

    public String column() {
        return column;
    }
}
