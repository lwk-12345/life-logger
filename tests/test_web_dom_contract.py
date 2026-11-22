from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class _DomContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.references: list[tuple[str, str, str]] = []
        self.local_assets: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = {key: value for key, value in attrs}
        node_id = values.get("id")
        if node_id:
            self.ids.append(node_id)

        for attribute in ("aria-labelledby", "aria-describedby", "aria-controls"):
            for reference in (values.get(attribute) or "").split():
                self.references.append((attribute, reference, node_id or tag))

        asset = values.get("src") if tag == "script" else None
        if tag == "link" and values.get("rel") == "stylesheet":
            asset = values.get("href")
        if asset and asset.startswith("/") and "://" not in asset:
            # [2026-10-14] 允许静态资产带 cache-busting 版本参数（/app.js?v=<mtime>）：
            # 归一化到裸路径再记录，契约断言的是「引用了哪些本地资产」，不含版本策略。
            self.local_assets.append(asset.split("?", 1)[0])


def test_primary_web_dom_contract_has_unique_ids_valid_aria_refs_and_assets() -> None:
    parser = _DomContractParser()
    parser.feed((ROOT / "web" / "index.html").read_text(encoding="utf-8"))

    duplicates = sorted(
        {node_id for node_id in parser.ids if parser.ids.count(node_id) > 1}
    )
    assert duplicates == []

    known_ids = set(parser.ids)
    missing = sorted(
        (attribute, reference, owner)
        for attribute, reference, owner in parser.references
        if reference not in known_ids
    )
    assert missing == []

    assert {"/style.css", "/journal_logic.js", "/app.js"} <= set(parser.local_assets)
    for asset in parser.local_assets:
        assert (ROOT / "web" / asset.lstrip("/")).is_file(), asset


def test_primary_runtime_smoke_anchors_remain_in_the_dom() -> None:
    parser = _DomContractParser()
    parser.feed((ROOT / "web" / "index.html").read_text(encoding="utf-8"))

    required = {
        "search-form",
        "search-summary",
        "search-results",
        "stats-surface",
        "stats-status",
        "stats-empty",
        "patterns-surface",
        "patterns-status",
        "patterns-results",
        "passport-stream-sessions",
        "passport-receiver-summary",
        "passport-receiver-health",
    }
    assert required <= set(parser.ids)
