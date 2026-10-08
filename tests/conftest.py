import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "collect" / "budongsan"))


@pytest.fixture
def contract() -> dict:
    """샘플 쇼핑몰 계약서 (eval/ 의 평가셋과 같은 것)."""
    return json.loads((ROOT / "eval" / "shoppingmall_contract.json").read_text(encoding="utf-8"))


@pytest.fixture
def compiler(contract):
    from compiler import Compiler
    return Compiler(contract)


@pytest.fixture
def compiler_for(contract):
    """드라이버만 바꾼 컴파일러."""
    from compiler import Compiler

    def make(driver: str):
        return Compiler({**contract, "driver": driver})
    return make
