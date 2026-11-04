import re
from pathlib import Path

import pytest

from console.model.kinds import State


CSS = (Path(__file__).resolve().parent.parent / "console/static/styles.css").read_text()


def test_every_component_state_has_a_stylesheet_selector():
    for state in State:
        assert f".state-{state.value}" in CSS


def test_every_html_page_gets_keyboard_search():
    from console.render.html import landing_page
    from console.index.graph import Index
    assert 'id="global-search"' in landing_page(Index())


def test_wide_tables_scroll_inside_their_own_box_not_the_page():
    from console.render.html import with_scrolling_tables
    page = ('<body><table><tr><td>a</td></tr></table>'
            '<table class="x"><tr><td>b</td></tr></table></body>')
    out = with_scrolling_tables(page)
    assert out.count('<div class="table-scroll"><table') == 2
    assert out.count("</table></div>") == 2
    assert ".table-scroll" in CSS and "overflow-x: auto" in CSS


def test_colours_are_tokens_redefined_for_dark_mode():
    # alpha-engine-config-I12783: one palette, both schemes.
    assert "--bad:" in CSS and "--good:" in CSS
    assert "@media (prefers-color-scheme: dark)" in CSS
    dark = CSS[CSS.index("@media (prefers-color-scheme: dark)"):]
    assert "--bad:" in dark and "--bg:" in dark


def test_a_row_state_renders_as_a_word_chip_and_a_first_cell_bar():
    # A `<tr>` border never renders, so the bar sits on the first cell, and
    # the state word is a chip carrying the same colour (§5.7: never colour only).
    from console.model.entity import Entity, Provenance
    from console.model.kinds import Kind, State
    from console.render.html import row
    ent = Entity(kind=Kind.COMPONENT, id="c", state=State.FAILED,
                 provenance=Provenance(source="s", as_of=None))
    out = row(ent)
    assert '<tr class="state-FAILED">' in out
    assert '<span class="chip">FAILED</span>' in out
    assert 'td:first-child { box-shadow: inset' in CSS


def test_a_source_reporting_failed_or_not_run_is_styled_not_neutral():
    # alpha-engine-config-I12983: the Crucible weekly run's stage rows carry
    # the producer's own `failed` / `not-run`; neither may render like `ok`.
    assert ".state-failed { --row-state: var(--bad)" in CSS
    assert ".state-not-run { --row-state: var(--off)" in CSS


# WCAG 2.2 AA (alpha-engine-config-I12783 closes-when): normal text needs 4.5:1
# against what it sits on. Measured here from the tokens themselves, in both
# schemes, so a palette edit that drops a pair below the line fails review
# instead of waiting for someone to run a checker on the live site.
_ROOT_BLOCKS = re.findall(r":root\s*\{([^}]*)\}", CSS)


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\b", block))


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


#: Every foreground token text is set in, against every background it is set
#: on: page, striped row, hovered row, and each state chip's own fill.
_TEXT_PAIRS = (
    [(fg, bg) for fg in ("text", "muted", "link") for bg in ("bg", "surface", "info-bg")]
    + [(s, bg) for s in ("bad", "warn", "good", "info", "off")
       for bg in ("bg", "surface", f"{s}-bg")]
)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_every_text_token_pair_meets_wcag_aa_contrast(scheme):
    light = _tokens(_ROOT_BLOCKS[0])
    palette = light if scheme == "light" else {**light, **_tokens(_ROOT_BLOCKS[1])}
    low = {f"{fg} on {bg}": round(_ratio(palette[fg], palette[bg]), 2)
           for fg, bg in _TEXT_PAIRS if _ratio(palette[fg], palette[bg]) < 4.5}
    assert not low, f"{scheme} pairs under 4.5:1: {low}"


def test_no_colour_literal_outside_the_token_blocks():
    """A hex colour outside `:root` cannot follow dark mode. `.ts-summary`,
    `.report-meta` and `.plan-blocker` once set light-mode text colours that
    read at 2.2-2.5:1 on the dark background."""
    rest = re.sub(r":root\s*\{[^}]*\}", "", CSS)
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", rest)


def test_tables_widen_with_the_window_and_keep_a_visible_scroll_bar():
    """Brian 2026-11-04: widening the browser shows more columns, and the
    sideways scroll bar is on screen and always visible, on every page."""
    import re
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / "console" / "static" / "styles.css").read_text()
    assert re.search(r"\.table-scroll \{[^}]*max-height: calc\(100vh", css)
    assert ".table-scroll::-webkit-scrollbar," in css
    for box in (".document.markdown {", ".report {"):
        rule = css.split(box, 1)[1].split("}", 1)[0]
        assert "max-width: none" in rule, box


def test_a_wide_table_has_a_sideways_bar_pinned_to_the_window():
    """The box's own bar is at its bottom edge, off screen part way down a
    long table (Brian 2026-11-04); console.js pins a synced copy instead."""
    js = (Path(__file__).resolve().parent.parent / "console/static/console.js").read_text()
    assert 'dock.className = "scroll-dock"' in js
    assert '".table-scroll, .md-table, .plan-scroll"' in js
    rule = re.search(r"\.scroll-dock \{([^}]*)\}", CSS).group(1)
    assert "position: fixed" in rule and "bottom: 0" in rule and "overflow-x: auto" in rule
