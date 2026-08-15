"""Kind is closed and identity is never console-minted — CN-2.1.

`console-policy.md` §2.1: everything rendered is a fact about exactly one of
seven kinds, each carrying an identifier assigned by its source of truth,
never by the console. The model already does both; these tests are the
chokepoint the clause file refused to credit without (alpha-engine-config-I7421).

The State vocabulary already has this shape in `test_state_vocabulary.py`.
Kind is the same obligation one layer up.
"""
from __future__ import annotations

import ast
import os
from dataclasses import MISSING, fields

from console.model.entity import Entity
from console.model.kinds import Kind

#: console-policy.md §2.1, transcribed. A second independent statement, so
#: adding an eighth kind has to be made twice and the second time is against
#: the policy text.
POLICY_SEVEN = {
    "component",
    "run",
    "cycle",
    "artifact",
    "signal",
    "decision",
    "incident",
}

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "console")
_NO_MINT_LAYERS = ("model", "index")
_MINT_CALLS = {"uuid4", "uuid1", "uuid3", "uuid5", "token_hex", "token_urlsafe"}


def test_kind_is_exactly_the_policy_seven():
    assert {k.value for k in Kind} == POLICY_SEVEN
    assert len(Kind) == 7


def test_kind_has_no_fall_through_member():
    """from_route returns None for an unknown segment; there is no OTHER."""
    assert Kind.from_route("other") is None
    assert Kind.from_route("unknown") is None
    assert Kind.from_route("") is None


def test_entity_id_has_no_default():
    """Identity is required at construction — a missing id cannot be minted."""
    id_field = next(f for f in fields(Entity) if f.name == "id")
    assert id_field.default is MISSING
    assert id_field.default_factory is MISSING


def test_no_identifier_minting_call_in_model_or_index():
    """uuid / secrets token helpers in the model or index layers would be
    the console assigning identity, which §2.1 forbids by name."""
    offenders = []
    for layer in _NO_MINT_LAYERS:
        root = os.path.join(_ROOT, layer)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                tree = ast.parse(open(path).read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            if alias.name.split(".")[0] == "uuid":
                                offenders.append(f"{layer}/{name}:{node.lineno}: import uuid")
                    if isinstance(node, ast.ImportFrom) and node.module == "uuid":
                        offenders.append(f"{layer}/{name}:{node.lineno}: from uuid import")
                    if isinstance(node, ast.Call):
                        func = node.func
                        called = (
                            func.attr if isinstance(func, ast.Attribute) else
                            func.id if isinstance(func, ast.Name) else None
                        )
                        if called in _MINT_CALLS:
                            offenders.append(f"{layer}/{name}:{node.lineno}: {called}()")
    assert offenders == [], (
        "model/index appears to mint identifiers (§2.1): " + "; ".join(offenders)
    )
