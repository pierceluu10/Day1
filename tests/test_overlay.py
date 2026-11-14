"""The overlay must work on pages whose CSP forbids inline script and style (Gitea sets a CSP)."""

from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

OVERLAY = Path(__file__).resolve().parent.parent / "intern_overlay.js"
PAGE = "http://csp.test/"
STRICT_CSP_HTML = "<h1>Add SSH Key</h1><input id=title style='margin:80px;width:300px'>"


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(bypass_csp=True)
        context.add_init_script(path=str(OVERLAY))
        page = context.new_page()
        page.route(
            PAGE,
            lambda route: route.fulfill(
                content_type="text/html",
                headers={"Content-Security-Policy": "default-src 'none'"},
                body=STRICT_CSP_HTML,
            ),
        )
        page.goto(PAGE)
        yield page
        browser.close()


RING_RECT_JS = """() => {
    const ring = document.querySelector('[data-dayone-overlay]').shadowRoot.querySelector('.ring');
    const r = ring.getBoundingClientRect();
    return {x: r.x, y: r.y, width: r.width, height: r.height, on: ring.classList.contains('on'),
            tag: ring.querySelector('.tag').textContent, color: getComputedStyle(ring).borderTopColor};
}"""
MODE_COLORS = {"memory": "rgb(22, 163, 74)", "reasoning": "rgb(217, 119, 6)", "moved": "rgb(220, 38, 38)"}


@pytest.mark.parametrize("mode", list(MODE_COLORS))
def test_ring_surrounds_target_in_mode_color(page, mode):
    page.evaluate("m => window.__dayone.mark({mode: m, selector: '#title', label: 'ada'})", mode)
    page.wait_for_timeout(400)  # transitions settle
    ring = page.evaluate(RING_RECT_JS)
    target = page.locator("#title").bounding_box()
    print(mode, ring, target)
    assert ring["on"] and ring["color"] == MODE_COLORS[mode]
    assert ring["x"] < target["x"] and ring["y"] < target["y"]
    assert ring["x"] + ring["width"] > target["x"] + target["width"]
    assert ring["y"] + ring["height"] > target["y"] + target["height"]
    assert ring["tag"].endswith("· ada")


TAG_BOX_JS = """() => {
    const tag = document.querySelector('[data-dayone-overlay]').shadowRoot.querySelector('.tag');
    const r = tag.getBoundingClientRect();
    return {top: r.top, bottom: r.bottom};
}"""


@pytest.mark.parametrize(("bbox", "side"), [([20, 80, 300, 30], "below"), ([20, 680, 300, 30], "above")])
def test_tag_sits_below_target_unless_no_room(page, bbox, side):
    # Below by default so it never covers the field's own label; above only near the viewport's bottom edge.
    page.evaluate("b => window.__dayone.mark({mode: 'moved', bbox: b})", bbox)
    page.wait_for_timeout(400)
    tag = page.evaluate(TAG_BOX_JS)
    top, bottom = bbox[1], bbox[1] + bbox[3]
    print(side, bbox, tag, page.viewport_size)
    assert tag["top"] > bottom if side == "below" else tag["bottom"] < top


def test_missing_target_frames_viewport_with_moved_label(page):
    page.evaluate("window.__dayone.mark({mode: 'moved', selector: '#gone'})")
    page.wait_for_timeout(400)
    ring = page.evaluate(RING_RECT_JS)
    print(ring)
    assert ring["tag"] == "This moved."
    assert ring["width"] > page.viewport_size["width"] * 0.9


def test_clear_hides_ring(page):
    page.evaluate("window.__dayone.mark({mode: 'memory', selector: '#title'})")
    page.evaluate("window.__dayone.clear()")
    assert page.evaluate(RING_RECT_JS)["on"] is False


def test_unknown_mode_is_rejected(page):
    with pytest.raises(PlaywrightError, match="unknown overlay mode"):
        page.evaluate("window.__dayone.mark({mode: 'bogus'})")
