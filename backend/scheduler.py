import logging
import os
import threading
from uuid import UUID

import db
from models import ApiException

log = logging.getLogger("nl2sql.scheduler")


class AutoSync:
    """한 번이라도 동기화한 데이터소스를 주기적으로 다시 동기화한다.

    AUTO_SYNC_MINUTES 가 0(기본)이면 돌지 않는다. 결과는 동기화 기록에 trigger='auto' 로
    남고, 깨진 지표가 생기면 경고 로그와 동기화 현황 화면에 드러난다.
    """

    def __init__(self, sync, interval_minutes: float):
        self._sync = sync
        self._interval = interval_minutes * 60
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def from_env(cls, sync) -> "AutoSync":
        raw = os.environ.get("AUTO_SYNC_MINUTES", "0").strip() or "0"
        try:
            minutes = float(raw)
        except ValueError:
            raise RuntimeError(f"AUTO_SYNC_MINUTES 는 숫자여야 합니다 (받은 값: {raw!r})") from None
        return cls(sync, minutes)

    @property
    def enabled(self) -> bool:
        return self._interval > 0

    def start(self) -> None:
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._loop, name="auto-sync", daemon=True)
        self._thread.start()
        log.info("자동 동기화 시작: %.0f분마다", self._interval / 60)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def run_once(self) -> None:
        for row in db.query("SELECT id, name FROM datasource WHERE synced_at IS NOT NULL ORDER BY name"):
            if self._stop.is_set():
                return
            try:
                self._sync(UUID(str(row["id"])), trigger="auto")
            except ApiException as error:
                log.warning("자동 동기화 실패: %s — %s", row["name"], error.detail)
            except Exception:
                log.exception("자동 동기화 실패: %s", row["name"])

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.run_once()
            except Exception:
                log.exception("자동 동기화 반복 중 오류")
