package com.nl2sql.compiler;

/** AST 를 SQL 로 바꿀 수 없다 (파이썬 AstError(ValueError)) */
public class AstError extends IllegalArgumentException {

    public AstError(String message) {
        super(message);
    }
}
