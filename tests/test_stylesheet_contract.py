from pathlib import Path

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
