"""Products — which product each page and dashboard belongs to.

Brian 2026-11-05: "organize product-related console pages together. For
instance, crucible, metron and vires should have grouped dashs. Then NE org
wide pages should be grouped together as well." The site index (`/index`)
used to group pages only by their `section:` (trading, models, ops, ...), so
Crucible's pages sat beside the fleet's operating-model pages, and Crucible's
dashboards beside the cost one.

A `products:` config block declares each product, in the order the index
lists them:

    products:
      crucible:
        title: Crucible
        about: the trading system
        panes: [crucible-v2, performance]   # dashboards (pane facets) it owns
        links:                               # pages that live outside the console
          - {title: Metron dash, url: https://metron.nousergon.ai/dash}

A page names its product with `product: <id>`. A page or dashboard that names
none is org-wide: it is listed under the org-wide group, never hidden.
Nothing about any product lives in this code; adding one is a config edit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

_ATTR = "_console_products"
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_LINK_URL = re.compile(r"^(https?://\S+|/\S*)$")

#: The group for everything no product claims.
ORG_WIDE_ID = "org-wide"
ORG_WIDE_TITLE = "Nous Ergon · org-wide"


class ProductsConfigError(ValueError):
    """A `products:` declaration this build cannot honour. Raised during
    `config.build_index`, so it fails the build by name."""


@dataclass(frozen=True)
class Product:
    id: str
    title: str
    about: str = ""
    #: Dashboards (`pane` facet values) this product owns.
    panes: tuple[str, ...] = ()
    #: (title, url) of pages that live outside the console, e.g. a product's
    #: own dashboard.
    links: tuple[tuple[str, str], ...] = ()


EMPTY: tuple[Product, ...] = ()


def _text(entry: Mapping[str, Any], key: str, where: str) -> str:
    value = str(entry.get(key) or "").strip()
    if not value:
        raise ProductsConfigError(f"{where}: `{key}` is required")
    return value


def parse(raw: Any) -> tuple[Product, ...]:
    """Validate and freeze the `products:` block, keeping declared order."""
    if raw is None:
        return EMPTY
    if not isinstance(raw, Mapping):
        raise ProductsConfigError(
            f"`products:` must be a mapping of product id to product, not {type(raw).__name__}")
    out: list[Product] = []
    owner: dict[str, str] = {}
    for pid, p in raw.items():
        pid = str(pid)
        where = f"product {pid!r}"
        if not _ID.match(pid) or pid == ORG_WIDE_ID:
            raise ProductsConfigError(
                f"{where}: id must be lowercase letters, digits and dashes, and not "
                f"{ORG_WIDE_ID!r} (the group for everything no product claims)")
        if not isinstance(p, Mapping):
            raise ProductsConfigError(f"{where}: must be a mapping")
        panes_raw = p.get("panes") or []
        if not isinstance(panes_raw, list) or not all(
                isinstance(x, str) and x.strip() for x in panes_raw):
            raise ProductsConfigError(f"{where}: `panes` must be a list of pane names")
        panes = tuple(x.strip() for x in panes_raw)
        for pane in panes:
            if pane in owner:
                # One dashboard, one group: listed twice it would read as two.
                raise ProductsConfigError(
                    f"{where}: pane {pane!r} is already claimed by product {owner[pane]!r}")
            owner[pane] = pid
        links_raw = p.get("links") or []
        if not isinstance(links_raw, list):
            raise ProductsConfigError(f"{where}: `links` must be a list of {{title, url}}")
        links = []
        for n, entry in enumerate(links_raw, start=1):
            at = f"{where} link #{n}"
            if not isinstance(entry, Mapping):
                raise ProductsConfigError(f"{at}: must be a mapping with `title` and `url`")
            url = _text(entry, "url", at)
            if not _LINK_URL.match(url):
                raise ProductsConfigError(f"{at}: `url` must be http(s):// or a /path, not {url!r}")
            links.append((_text(entry, "title", at), url))
        out.append(Product(id=pid, title=_text(p, "title", where),
                           about=str(p.get("about") or "").strip(),
                           panes=panes, links=tuple(links)))
    return tuple(out)


def check_pages(products: tuple[Product, ...], pages: Any) -> None:
    """A page naming an undeclared product fails the build by name, rather
    than landing in the org-wide group under a product it does not belong to."""
    known = {p.id for p in products}
    for page in pages:
        if page.product and page.product not in known:
            raise ProductsConfigError(
                f"page {page.id!r}: `product: {page.product}` is not declared under "
                f"`products:` (declared: {', '.join(sorted(known)) or 'none'})")


def attach(index: Any, products: tuple[Product, ...]) -> None:
    setattr(index, _ATTR, products)


def declared(index: Any) -> tuple[Product, ...]:
    return getattr(index, _ATTR, EMPTY)


def pane_owner(products: tuple[Product, ...]) -> dict[str, str]:
    """pane facet value -> product id, for the panes some product claims."""
    return {pane: p.id for p in products for pane in p.panes}
