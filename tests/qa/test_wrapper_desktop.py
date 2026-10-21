"""The factory's tools for a remote desktop's live view: it opens at the desktop's own size, keys go one by one
with Shift held for capitals and symbols, and a click rests, then holds the button."""

from __future__ import annotations

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


def test_a_desktop_click_rests_at_its_point_then_holds_the_button_as_the_desktop_takes_a_click() -> None:
    browser = Browser([])
    said, failed = browser.answer("browser_mouse_press_xy", {"x": 100, "y": 200.5})
    rest = "await page.mouse.move(100, 200.5); await page.waitForTimeout(250);"
    held = "await page.mouse.down(); await page.waitForTimeout(150); await page.mouse.up();"
    code = f"async (page) => {{ {rest} {held} }}"
    assert browser.calls == [("browser_run_code_unsafe", {"code": code})]
    assert (said, failed) == ("Pressed at 100, 200.5. A screenshot shows what it did.", False)
