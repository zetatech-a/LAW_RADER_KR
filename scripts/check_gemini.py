"""메일/state 없이 실제 설정과 키로 요약 경로를 검증한다."""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.models import Post
from src.summarizer import Summarizer, classify_error


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config().llm
    summarizer = Summarizer(cfg)
    report = ["## Gemini 실제 연결 검증", f"모델 순서: {', '.join(cfg.model_chain)}"]
    if not summarizer.available:
        report.append("실패: GEMINI_API_KEY가 없거나 요약이 비활성화되어 있습니다.")
        result = 2
    else:
        # 검증용 가상 원문이다. 실제 규제 정보로 사용하지 않는다.
        post = Post(source_key="fsc_press", source_name="연결 검증용 가상 기관",
                    post_id="connection-check", title="검증용 가상 규정 개정",
                    url="https://example.com/connection-check",
                    body="검증용 가상 규정은 금융회사를 대상으로 보고 절차를 개선한다. "
                         "금융회사는 변경된 양식으로 월별 보고서를 제출해야 한다. "
                         "해당 규정은 2027년 1월 1일부터 적용된다. "
                         "이 문서는 실제 법령이 아닌 연결 검증용 가상 본문이다.")
        try:
            lines = summarizer.summarize(post, time.monotonic() + 180)
            if len(lines) != cfg.lines:
                raise ValueError(f"요약 문장 수 불일치: {len(lines)} / {cfg.lines}")
            report.append(f"성공: {summarizer._active_model}, {len(lines)}줄 요약 생성")
            report.extend(f"- {line}" for line in lines)
            result = 0
        except Exception as exc:
            report.append(f"실패 유형: {classify_error(exc).value}")
            report.append(str(exc))
            result = 2
    rendered = "\n\n".join(report) + "\n"
    print(rendered)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as output:
            output.write(rendered)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
