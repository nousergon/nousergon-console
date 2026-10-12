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
