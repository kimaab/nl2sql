package com.nl2sql.common;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;

/** 파이썬 isoformat() 과 같은 시각 글. 초는 늘 쓰고, 마이크로초는 0 이 아닐 때만 쓴다. */
public final class Times {

    private static final DateTimeFormatter SECONDS = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss");
    private static final DateTimeFormatter DAY = DateTimeFormatter.ofPattern("yyyy-MM-dd");

    private Times() {
    }

    public static String iso(OffsetDateTime value) {
        if (value == null) {
            return null;
        }
        return local(value.toLocalDateTime(), 'T') + offset(value.getOffset());
    }

    public static String iso(LocalDateTime value) {
        return value == null ? null : local(value, 'T');
    }

    /** isoformat(sep=" ", timespec="seconds") */
    public static String seconds(LocalDateTime value) {
        return value.format(SECONDS).replace('T', ' ');
    }

    public static String seconds(OffsetDateTime value) {
        return seconds(value.toLocalDateTime()) + offset(value.getOffset());
    }

    public static String day(LocalDate value) {
        return value.format(DAY);
    }

    public static String time(LocalTime value) {
        String text = value.withNano(0).toString();
        if (text.length() == 5) {
            text += ":00";
        }
        int micros = value.getNano() / 1000;
        return micros == 0 ? text : text + String.format(".%06d", micros);
    }

    private static String local(LocalDateTime value, char sep) {
        String text = value.format(SECONDS).replace('T', sep);
        int micros = value.getNano() / 1000;
        return micros == 0 ? text : text + String.format(".%06d", micros);
    }

    private static String offset(ZoneOffset offset) {
        String id = offset.getId();
        return id.equals("Z") ? "+00:00" : id;
    }
}
