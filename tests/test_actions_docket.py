"""Decision-only body projections and grouped docket rendering."""
from datetime import date

import pytest

from console.index import actions
from console.render import html
from tests.test_actions import _index, _issue


@pytest.mark.parametrize(('text', 'label', 'reason'), [
    ('My pick: Ship — reversible', 'My pick', 'Ship — reversible'),
    ('Best-in-class: Wait', 'Best-in-class', 'Wait'),
    ('System-optimized: Retry', 'System-optimized', 'Retry'),
    ('Other: Skip', 'Other', 'Skip'),
    ('Keep the current policy', 'B', 'Keep the current policy'),
])
def test_option_card_labels(text, label, reason):
    assert actions.option_card('B', text) == (label, reason)


@pytest.mark.parametrize(('body', 'expected', 'state'), [
    ('No date.', None, None),
    ('Due: 2026-11-16', '2026-11-16', 'overdue'),
    ('Re-exam: 2026-11-17', '2026-11-17', 'today'),
    ('Default: A after 2026-11-20', '2026-11-20', 'soon'),
    ('Due: 2026-11-21', '2026-11-21', 'later'),
    ('Due: 2026-02-30', None, None),
    ('Re-exam: 2026-11-18\nDue: 2026-11-16', '2026-11-18', 'soon'),
    ('Default: A after 2026-11-19\nRe-exam: 2026-11-16', '2026-11-19', 'soon'),
])
def test_decision_due(body, expected, state):
    due = actions.decision_due(body)
    assert due == expected
    assert actions.due_class(due, date(2026, 11, 17)) == state


def test_what_this_is():
    assert actions.what_this_is('Opening.\n\n**What this is**\n\nA **plain** [description](https://example.test).\n\n**Options:** A) one B) two') == 'A plain description.'
    assert actions.what_this_is('First paragraph.\nStill first.\n\nSecond.') == 'First paragraph. Still first.'
    assert actions.what_this_is(None) == ''


OPTIONS = '\n\n**Options:** A) My pick: Ship — reversible B) Other: Wait — more evidence\n\n**Recommendation:** A — reversible.'


def test_groups_preserve_listing_order_with_other_last():
    idx, _ = _index(issues=[
        _issue(1, labels=('gate:operator',), body='Other.' + OPTIONS),
        _issue(2, labels=('gate:operator', 'area:zeta'), body='Zeta.' + OPTIONS),
        _issue(3, labels=('gate:operator', 'area:alpha'), body='Alpha.' + OPTIONS),
        _issue(4, labels=('gate:operator', 'area:zeta'), body='Zeta again.' + OPTIONS),
    ])
    found = actions.listing(idx)
    assert [(name, [a.number for a in rows]) for name, rows in found.decision_groups] == [
        ('Zeta', [2, 4]), ('Alpha', [3]), ('Other', [1])]


def test_three_decisions_two_projects(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(html, 'render_now', lambda: datetime(2026, 11, 17, tzinfo=timezone.utc))
    idx, _ = _index(issues=[
        _issue(1, labels=('gate:operator', 'area:atlas'), body='First.\n\nDue: 2026-11-16' + OPTIONS),
        _issue(2, labels=('gate:operator', 'area:atlas'), body='Second.\n\nRe-exam: 2026-11-17' + OPTIONS),
        _issue(3, labels=('gate:operator', 'area:beacon'), body='Third.' + OPTIONS),
    ])
    page = html.actions_page(idx)
    assert '<h3 class="decision-project">Atlas</h3>' in page
    assert '<h3 class="decision-project">Beacon</h3>' in page
    assert page.count('class="chip decision-due ') == 2
    assert 'decision-due overdue' in page and 'decision-due today' in page
    assert page.count('data-docket-choice=') == 6
    assert page.count('aria-pressed="true"') == 3
    assert '3 decisions, all as recommended' in page
    assert 'No brief written' not in page


def test_decision_copy_blocks_and_escaping():
    idx, _ = _index(issues=[_issue(1, title='<script>bad</script>', body=(
        '**What this is:** A <tag>.\n\n**Links:** [guide](https://example.test/guide)\n\n'
        'Run locally:\n```sh\necho safe\n```' + OPTIONS))])
    page = html.actions_page(idx)
    card = page[page.index('<article id="a1"'):page.index('</article>')]
    assert '<script>bad</script>' not in card
    assert 'A &lt;tag&gt;.' in card
    assert '<code>cd ~ &amp;&amp; echo safe</code>' in card
    assert 'aria-label="Copy this link"' in card
    assert 'aria-label="Copy this command"' in card


def test_docket_script_summary_and_partial_retry():
    import json
    import subprocess
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / 'console/static/console.js'
    code = r'''
const assert = require('node:assert/strict');
const {docketSummary, submitDocket} = require(SCRIPT);
const rows = [
  {title:'One', recommended:'A', choice:'A', text:'My pick: Ship'},
  {title:'Two', recommended:'B', choice:'B', text:'Wait'},
  {title:'Three', recommended:'A', choice:'A', text:'Ship'}
];
assert.equal(docketSummary(rows), '3 decisions, all as recommended');
rows[1].choice = 'A'; rows[1].text = 'Other: Ship';
assert.equal(docketSummary(rows), 'Two: Other: Ship');
rows[1].choice = 'B';
assert.equal(docketSummary(rows), '3 decisions, all as recommended');
assert.equal(docketSummary([{title:'Missing', choice:'', recommended:''}]), '1 decisions need a selection');
(async () => {
  const calls = [];
  await submitDocket(rows, async row => {calls.push(row.title); row.saved = row.title !== 'Two';});
  assert.deepEqual(calls, ['One', 'Two', 'Three']);
  calls.length = 0;
  await submitDocket(rows, async row => {calls.push(row.title); row.saved = true;});
  assert.deepEqual(calls, ['Two']);
})();
'''.replace('SCRIPT', json.dumps(str(script)))
    subprocess.run(['node', '-e', code], check=True, capture_output=True, text=True)


@pytest.mark.parametrize('body', ['**Due:** 2026-11-18', '**Re-exam:** 2026-11-18', '**Due**: 2026-11-18', '- **Default:** A after 2026-11-18'])
def test_bold_due_labels(body):
    assert actions.decision_due(body) == '2026-11-18'


def test_agent_handoff_releases_docket_refresh_hold():
    """Run the real update function with no cards after the final agent handoff."""
    import json
    import subprocess
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'console/static/console.js').read_text()
    update = source[source.index('  function updateDocket() {'):source.index('\n  document.addEventListener("click"', source.index('  function updateDocket() {'))]
    harness = '''
const assert = require('node:assert/strict');
let docketDirty = true, docketSubmitting = false;
let rows = [{saved:false,form:{}}];
const docketRows = () => rows;
const docketSummary = () => '';
const button = {}, summary = {};
const document = {querySelector: () => ({querySelector: selector => selector === '.docket-submit' ? button : summary})};
UPDATE
updateDocket();
assert.equal(docketDirty, true); // A remaining local pick must survive refresh.
rows = [];
updateDocket();
assert.equal(docketDirty, false); // Handoff removed the final local pick.
assert.equal(button.disabled, true);
'''.replace('UPDATE', update)
    subprocess.run(['node', '-e', harness], check=True, capture_output=True, text=True)
