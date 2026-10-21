"""The factory's tools for a remote desktop's live view: it opens at the desktop's own size, keys go one by one
with Shift held for capitals and symbols, and a click rests, holds the button, then answers once the view's picture
changed, or says it saw none."""

from __future__ import annotations

import pytest
from fake_browser import Browser


def test_the_desktop_opens_at_its_own_size() -> None:
    browser = Browser([])
    browser.answer("browser_open_desktop", {"url": "http://127.0.0.1:6080/vnc.html"})
    assert browser.calls == [
        ("browser_resize", {"width": 1280, "height": 800}),
        ("browser_navigate", {"url": "http://127.0.0.1:6080/vnc.html"}),
    ]


def test_typed_keys_hold_shift_for_capitals_and_symbols_and_the_text_never_runs_as_code() -> None:
    browser = Browser([])
    said, failed = browser.answer("browser_type_keys", {"text": 'Hi "x'})
    keys = '[["press", "Shift+KeyH"], ["type", "i"], ["type", " "], ["press", "Shift+Quote"], ["type", "x"]]'
    each = "await page.keyboard[how](key); await page.waitForTimeout(40);"
    code = f"async (page) => {{ for (const [how, key] of {keys}) {{ {each} }} }}"
    assert browser.calls == [("browser_run_code_unsafe", {"code": code})]
    assert not failed and said.startswith("Typed 5 keys. A desktop may draw them seconds late")


def test_a_desktop_click_rests_at_its_point_looks_holds_the_button_then_watches_the_picture_for_its_wait() -> None:
    browser = Browser([], (1240,))
    browser.answer("browser_mouse_press_xy", {"x": 100, "y": 200.5})
    [(name, args)] = browser.calls
    rest = "await page.mouse.move(100, 200.5); await page.waitForTimeout(250); const before = await look();"
    held = "await page.mouse.down(); await page.waitForTimeout(150); await page.mouse.up();"
    watch = "while (Date.now() - start < 6000) { await page.waitForTimeout(200); const now = await look();"
    code = args["code"]
    assert name == "browser_run_code_unsafe" and code.index(rest) < code.index(held) < code.index(watch)
    assert "drawImage(video, 0, 0, w, h)" in code and "[32, 20]" in code and "Math.abs(v - before[i]) > 12" in code


@pytest.mark.parametrize(
    ("seen", "says"),
    [
        (1240, "The desktop changed 1.2 s after the press: a screenshot shows what it did."),
        ("same", "No change on the desktop within 6 s: take a screenshot before you click again."),
        ("unread", "The factory cannot read this view's picture, so it waited 6 s: take a screenshot before you click "
         "again."),
        ("none", "A screenshot shows what it did."),
    ],
    ids=["the-picture-changed", "it-stayed-the-same", "it-cannot-be-read", "no-live-view"],
)  # fmt: skip
def test_a_desktop_click_says_whether_the_desktop_drew_a_change_within_its_wait(seen: int | str, says: str) -> None:
    assert Browser([], (seen,)).answer("browser_mouse_press_xy", {"x": 100, "y": 200.5}) == (
        f"Pressed at 100, 200.5. {says}", False)  # fmt: skip
