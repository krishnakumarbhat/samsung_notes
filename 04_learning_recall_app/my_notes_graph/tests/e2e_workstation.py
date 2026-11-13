"""Browser-contract QA: real local GETs, mocked learning POSTs, no API spending/history writes."""
from __future__ import annotations
import json
import time
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright


def main() -> None:
    """Exercise both exam modes, one-question tutor, synthesis, citations and rich history."""
    report = {"mocked_learning_posts": True, "real_api_spend": 0, "checks": {}, "errors": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True,
                                             args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: report["errors"].append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())
        mode, turns = "practice", 0
        selected = {}
        source = {"source": "", "page": 1}
        questions = []

        def result():
            rows = [{**q, "selected": selected.get(q["id"]), "correct": selected.get(q["id"]) == 1,
                     "correct_index": 1, "explanation": "Differentiate: $2x=6$.",
                     "error_tag": "" if selected.get(q["id"]) == 1 else "Forgot Formula",
                     "fsrs": {"stability_before": 1, "stability": 3, "due": "2026-11-01"}}
                    for q in questions]
            return {"score": sum(q["correct"] for q in rows), "total": len(rows), "results": rows}

        def api_route(route):
            nonlocal mode, turns, questions
            request = route.request
            path = urlparse(request.url).path
            if path == "/api/learning/history":
                route.fulfill(json={"sessions": [{"id": "contract-test", "kind": "exam", "created": time.time(),
                                 "finished": True, "data": {"mode": mode, "result": result()}}]})
                return
            if request.method != "POST":
                route.continue_(); return
            body = request.post_data_json or {}
            if path == "/api/exam/start":
                mode = body["mode"]
                selected.clear()
                questions = [{"id": i, "question": f"Question {i}: derive $x^2$ at x=3.",
                              "options": ["3", "6", "9", "12"], "source": source} for i in range(1,26)]
                payload = {"id": "mock-exam", "mode": mode, "deadline": time.time() + 300, "questions": questions}
            elif path == "/api/exam/answer":
                selected[body["question_id"]] = body.get("choice")
                payload = {"saved": True}
                if mode == "practice" and body.get("choice") is not None:
                    payload["feedback"] = {"correct": body["choice"] == 1, "correct_index": 1,
                                           "explanation": "The derivative is $2x=6$.", "source": source}
            elif path == "/api/exam/finish":
                payload = result()
            elif path == "/api/tutor/step":
                turns += 1
                payload = {"session_id": "mock-tutor", "explanation": "Think of a speedometer." if turns == 1 else "Now picture filling a bucket.",
                           "question": "What is the instantaneous rate?", "feedback": "Try a new analogy." if turns > 1 else "",
                           "sources": [source]}
            elif path == "/api/studio":
                payload = {"answer": "$x^2$\n```python\nprint(1)\n```\n<img src=x onerror=alert(1)>", "sources": [source]}
            else:
                raise AssertionError(f"Unexpected learning POST: {path}")
            route.fulfill(json=payload)

        page.route("**/api/**", api_route)
        try:
            page.goto("http://127.0.0.1:8787/", wait_until="networkidle")
            page.wait_for_selector("#srclist input[type=checkbox]")
            source["source"] = page.evaluate("state.sources.find(s=>s.status==='searchable').source")
            page.evaluate("app.setMode('practice'); app.startQuiz()")
            page.wait_for_selector("#palette .palbtn")
            assert page.locator("#palette .palbtn").count() == 25
            page.locator("#examopts .opt").nth(1).click()
            page.wait_for_function("() => document.querySelector('#examfeedback').textContent.includes('Correct')")
            page.get_by_role("button", name="Clear", exact=True).click()
            page.wait_for_function("() => !document.querySelector('#examfeedback').textContent.trim()")
            page.locator("#examopts .opt").nth(1).click()
            page.wait_for_function("() => document.querySelector('#examfeedback').textContent.includes('Correct')")
            page.get_by_role("button", name="Finish", exact=True).click()
            page.wait_for_function("() => document.querySelector('#quizcard').textContent.includes('Score')")
            report["checks"]["practice_palette_clear_feedback_finish"] = True

            page.evaluate("app.setMode('exam'); app.startQuiz()")
            page.wait_for_selector("#palette .palbtn")
            page.locator("#examopts .opt").nth(1).click()
            page.wait_for_timeout(200)
            assert not page.locator("#examfeedback").inner_text().strip()
            assert page.locator("#examtimer").count() == 1
            page.get_by_role("button", name="Finish", exact=True).click()
            page.wait_for_function("() => document.querySelector('#quizcard').textContent.includes('Score')")
            report["checks"]["exam_hides_solutions_until_finish"] = True

            page.locator("nav button[data-p=tutor]").click()
            page.locator("#tutorq").fill("Teach derivatives")
            page.locator("#tutorsend").click()
            page.wait_for_selector(".verify-row input")
            page.locator(".verify-row input:enabled").fill("I am unsure")
            page.locator(".verify-row button:enabled").click()
            page.wait_for_function("() => document.querySelector('#tutorchat').textContent.includes('bucket')")
            assert page.locator(".verify-row input:enabled").count() == 1
            report["checks"]["tutor_one_active_verification_new_analogy"] = True

            page.locator("nav button[data-p=studio]").click()
            page.get_by_role("button", name="Cheatsheet", exact=True).click()
            page.wait_for_selector("#studioout .katex")
            assert page.locator("#studioout code.hljs").count() == 1
            assert page.locator("#studioout img").count() == 0
            report["checks"]["one_click_synthesis_math_code_and_escaping"] = True

            page.locator("nav button[data-p=history]").click()
            page.wait_for_selector("#histwrap .history-session")
            page.locator("#histwrap .history-session > summary").first.click()
            page.locator("#histwrap .history-session .history-question > summary").first.click()
            assert page.locator("#histwrap .history-session .history-full li").count() == 100
            assert "FSRS stability" in page.locator("#histwrap .history-session").inner_text()
            report["checks"]["rich_history_options_solutions_fsrs_citations"] = True
            assert not report["errors"], report["errors"]
            report["passed"] = True
        finally:
            destination = Path(__file__).resolve().parents[3] / "00_orchestrator/STAGE2_BROWSER_MODES_QA.json"
            destination.write_text(json.dumps(report, indent=2))
            print(json.dumps(report))
            browser.close()


if __name__ == "__main__":
    main()
