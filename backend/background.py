"""오래 걸리는 작업(지표 자동 등록 · 지표 검증)을 요청과 떼어 스레드에서 돌린다.

실행 기록은 메타데이터 DB 에 남고, 화면은 기록을 다시 읽어 진행을 본다.
지금 도는 실행은 이 프로세스의 메모리에만 있다 — 서버가 재시작되면 'running' 으로 남은 기록은 중단된 것이다.
"""

import logging
import threading

log = logging.getLogger("nl2sql.background")

_lock = threading.Lock()
_active: dict[str, str] = {}  # 실행 id → 잠금 키 (작업 종류:시스템 id)


def is_active(run_id) -> bool:
    with _lock:
        return str(run_id) in _active


def start(key: str, run_id, work, on_error) -> bool:
    """work() 를 스레드에서 돌린다. 같은 key 가 이미 돌면 시작하지 않고 False.

    work 가 예외로 끝나면 on_error(오류) 를 부른다 — 실행 기록을 'error' 로 닫는 데 쓴다.
    """
    run = str(run_id)
    with _lock:
        for k in _active.values():
            if k == key:
                return False
        _active[run] = key

    def target():
        try:
            work()
        except Exception as error:
            log.exception("%s 실행 %s 실패", key, run)
            try:
                on_error(error)
            except Exception:
                log.exception("%s 실행 %s 의 실패 기록을 남기지 못했습니다", key, run)
        finally:
            with _lock:
                _active.pop(run, None)

    threading.Thread(target=target, name=f"{key.split(':')[0]}-{run[:8]}", daemon=True).start()
    return True
