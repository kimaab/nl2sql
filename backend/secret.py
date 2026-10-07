"""대상 DB 비밀번호 암호화. 동기화 때 실제로 접속해야 하므로 복원 가능해야 한다 (AES-256-GCM).

키는 환경변수 NL2SQL_SECRET_KEY (32바이트를 base64 로). 만들기:
    python -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"
키를 잃으면 저장된 비밀번호를 풀 수 없다 — 시스템마다 비밀번호를 다시 입력해야 한다.
"""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE = 12


def _key() -> bytes:
    raw = os.environ.get("NL2SQL_SECRET_KEY", "").strip()
    if not raw:
        raise RuntimeError("NL2SQL_SECRET_KEY 가 없습니다 (backend/.env). 만드는 법은 secret.py 맨 위 주석")
    try:
        key = base64.b64decode(raw, validate=True)
    except ValueError:
        raise RuntimeError("NL2SQL_SECRET_KEY 는 base64 여야 합니다") from None
    if len(key) != 32:
        raise RuntimeError(f"NL2SQL_SECRET_KEY 는 32바이트여야 합니다 (받은 것: {len(key)}바이트)")
    return key


def check() -> None:
    """기동 때 키가 쓸 만한지 확인한다 — 첫 동기화에서야 터지면 원인을 찾기 어렵다."""
    _key()


def encrypt(plain: str) -> bytes:
    nonce = os.urandom(NONCE)
    return nonce + AESGCM(_key()).encrypt(nonce, plain.encode("utf-8"), None)


def decrypt(blob: bytes) -> str:
    blob = bytes(blob)
    return AESGCM(_key()).decrypt(blob[:NONCE], blob[NONCE:], None).decode("utf-8")
