"""alpha-engine-config-I12535: a check's finding rows render on its page.

The checks-envelope adapter used to keep only `findings_count`, so a check
whose findings ARE its answer (an account registry: one row per account, with
the date its root MFA was last verified) rendered as a bare number. The rows
now ride on the component and render as a table: worst first where a row
declares a state (I11805's order), a cell the check did not write shown as
not stated, and a count line that names every row.

Fixtures are synthetic (this repo is public): no real account ids or emails.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from console.adapters import checks_envelope
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import json as render_json
from console.render.html import entity_page, findings_section
from console.server.app import _html
from console.server.router import resolve

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
KEY = "ops/checks/acct-registry/latest.json"

#: The shape the fleet's account-registry check writes: one finding per
#: account, keyed by the account's registry name, with its state and root-MFA
#: verification date inside `detail`, plus an organization-level row.
ACCOUNTS = {
    "schema_version": 1,
    "check_id": "acct-registry",
    "label": "Accounts",
    "ran_at": "2026-10-26T11:40:00+00:00",
    "status": "attention",
    "summary": "3 accounts; 2 finding(s); no dated root-MFA verification",
    "cadence_minutes": 1440,
    "findings": [
        {"key": "acct-a", "detail": "acct-a (000000000001): ACTIVE; root MFA verified never"},
        {"key": "acct-b", "detail": "acct-b (000000000002): ACTIVE; root MFA verified 2026-10-20"},
        {"key": "acct-c", "detail": "acct-c (000000000003): not a member yet; root MFA verified never"},
    ],
}


def _fetch(*bodies):
    store = {f"ops/checks/{b['check_id']}/latest.json": b for b in bodies}
    return checks_envelope.fetch(
        {"bucket": "fixture-bucket", "prefix": "ops/checks/"},
        lister=lambda bucket, prefix: [(k, b["ran_at"]) for k, b in store.items()],
        reader=lambda bucket, key: json.loads(json.dumps(store[key])),
        now=NOW,
    )


def _component(result, cid):
    return next(e for e in result.entities if e.kind is Kind.COMPONENT and e.id == cid)


def _index(result) -> Index:
    index = Index()
    index.add_result(result)
    index.finalize()
    return index


def _rows(html: str) -> list[list[str]]:
    section = html.split("<h2>findings</h2>", 1)[1].split("</table>", 1)[0]
    return [re.findall(r"<td[^>]*>(.*?)</td>", tr)
            for tr in re.findall(r"<tr>(.*?)</tr>", section) if "<td" in tr]


def test_adapter_carries_the_rows_in_the_order_the_check_wrote_them():
    comp = _component(_fetch(ACCOUNTS), "acct-registry")
    assert comp.detail["findings_count"] == 3
    assert [r["key"] for r in comp.detail["findings"]] == ["acct-a", "acct-b", "acct-c"]
    assert comp.detail["findings"][1]["detail"].endswith("root MFA verified 2026-10-20")


def test_the_accounts_page_lists_one_row_per_account_with_its_verified_date():
    result = _fetch(ACCOUNTS)
    index = _index(result)
    page = _html(index, resolve("/component/acct-registry", ""))
    assert "<h2>findings</h2>" in page
    rows = _rows(page)
    assert [r[0] for r in rows] == ["acct-a", "acct-b", "acct-c"]
    assert "root MFA verified 2026-10-20" in rows[1][1]
    assert "showing 3 of 3 findings" in page


def test_the_rows_reach_the_json_representation_of_the_same_url():
    index = _index(_fetch(ACCOUNTS))
    payload = render_json.payload(index, resolve("/component/acct-registry", ""))
    text = json.dumps(payload)
    assert "root MFA verified 2026-10-20" in text


def test_rows_with_a_declared_state_read_worst_first_and_are_counted():
    body = dict(ACCOUNTS, findings=[
        {"key": "acct-a", "state": "ok", "verified": "2026-10-20"},
        {"key": "acct-b", "state": "error", "verified": None},
        {"key": "acct-c", "state": "attention"},
        {"key": "acct-d", "state": "ok", "verified": "2026-10-01"},
    ])
    html = findings_section(_component(_fetch(body), "acct-registry"))
    rows = _rows(html)
    assert [r[0] for r in rows] == ["acct-b", "acct-c", "acct-a", "acct-d"]
    # state column leads after key; healthy rows keep the check's order
    assert re.search(r"<th>key</th><th>state</th><th>verified</th>", html)
    counts = re.search(r'<p class="list-shown">(.*?)</p>', html).group(1)
    assert counts.index("1 error") < counts.index("1 attention") < counts.index("2 ok")


def test_a_value_the_check_did_not_write_renders_as_not_stated():
    body = dict(ACCOUNTS, findings=[
        {"key": "acct-a", "verified": "2026-10-20"},
        {"key": "acct-b", "verified": None},
        {"key": "acct-c"},
    ])
    rows = _rows(findings_section(_component(_fetch(body), "acct-registry")))
    assert rows[0][1] == "2026-10-20"
    assert "not stated" in rows[1][1] and "not stated" in rows[2][1]


def test_no_findings_is_said_not_left_blank():
    body = dict(ACCOUNTS, status="ok", findings=[])
    html = findings_section(_component(_fetch(body), "acct-registry"))
    assert "No findings — the check reported none." in html


def test_rows_past_the_cap_are_counted_not_silently_dropped():
    n = checks_envelope.FINDINGS_CARRIED + 7
    body = dict(ACCOUNTS, findings=[{"key": f"k{i}", "detail": "d"} for i in range(n)])
    comp = _component(_fetch(body), "acct-registry")
    assert len(comp.detail["findings"]) == checks_envelope.FINDINGS_CARRIED
    assert comp.detail["findings_count"] == n
    html = findings_section(comp)
    assert f"showing {checks_envelope.FINDINGS_CARRIED} of {n} findings" in html
    assert "7 not carried into the index" in html


def test_non_dict_and_nested_findings_are_kept_and_rendered_on_one_line():
    body = dict(ACCOUNTS, findings=["a bare string",
                                    {"key": "x", "scps": ["p1", "p2"], "ids": {"a": 1}}])
    comp = _component(_fetch(body), "acct-registry")
    rows = comp.detail["findings"]
    assert rows[0] == {"detail": "a bare string"}
    assert rows[1]["scps"] == ["p1", "p2"]  # structure kept for the JSON view
    cells = _rows(findings_section(comp))
    assert "p1, p2" in cells[1] and "a=1" in cells[1]


def test_a_component_from_another_source_renders_no_findings_section():
    ent = Entity(kind=Kind.COMPONENT, id="svc", state=State.HEALTHY,
                 provenance=Provenance(source="fixture", as_of="2026-10-26T10:00:00Z"))
    assert findings_section(ent) == ""
    index = Index()
    index.add_result(AdapterResult(name="f", status=AdapterStatus.OK,
                                   claim_class=ClaimClass.DECLARATION,
                                   entities=(ent,), edges=()))
    index.finalize()
    assert "<h2>findings</h2>" not in entity_page(index, index.entity("svc"))


def test_cells_are_escaped():
    body = dict(ACCOUNTS, findings=[{"key": "<b>x</b>", "detail": "<script>"}])
    html = findings_section(_component(_fetch(body), "acct-registry"))
    assert "<script>" not in html and "&lt;script&gt;" in html
