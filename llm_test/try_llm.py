"""request.json 을 LLM 에 N 번 보내고, 응답이 JSON 으로 읽히는지 본다.

python llm_test/try_llm.py [횟수]
"""
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv(Path(__file__).parent.parent / "backend" / ".env")
URL = os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions"
KEY = os.environ.get("LLM_API_KEY", "not-needed")

body = json.loads((Path(__file__).parent / "request.json").read_text(encoding="utf-8"))
for i in range(1, int(sys.argv[1]) + 1 if len(sys.argv) > 1 else 4):
    started = time.monotonic()
    http = requests.post(URL, json=body, headers={"Authorization": f"Bearer {KEY}"}, timeout=600)
    res = http.json()
    if "choices" not in res:
        print(f"[{i}] HTTP {http.status_code} 오류 응답: {res}")
        continue
    choice = res["choices"][0]
    text = choice["message"].get("content") or ""
    reasoning = choice["message"].get("reasoning_content") or choice["message"].get("reasoning") or ""
    body_text = text.strip().removeprefix("```json").removesuffix("```").strip()
    try:
        print(f" bodys {text}")
        json.loads(body_text)
        verdict = "JSON OK"
    except ValueError as e:
        verdict = f"JSON 깨짐: {e}"
    print(f"[{i}] {time.monotonic() - started:.0f}s finish={choice.get('finish_reason')} "
          f"usage={res.get('usage')} 추론 {len(reasoning)}자 / 본문 {len(text)}자 -> {verdict}")
    if verdict != "JSON OK":
        print(text or "(본문 비어 있음) 추론 끝부분:\n" + reasoning[-500:])
