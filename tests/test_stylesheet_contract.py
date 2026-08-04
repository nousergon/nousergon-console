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
