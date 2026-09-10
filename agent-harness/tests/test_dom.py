from __future__ import annotations

from harness.healing.dom import clean_html


def big_page(rows: int = 400) -> str:
    """Roughly the shape of a real checkout page.

    Proportions matter more than absolute size here: on a real vendor page the
    bundled script and inline style dwarf the markup, which is exactly why
    stripping them is worth a stage in the pipeline.
    """
    script = "var x = " + ("0123456789" * 30_000) + ";"
    style = ".a{color:#fff}" * 5_000
    body = "".join(
        f'<div class="row r{i}" style="color:red" onclick="track({i})" data-analytics="{i}">'
        f'<span aria-label="row {i}">Row {i}</span></div>'
        for i in range(rows)
    )
    return (
        f"<html><head><style>{style}</style><script>{script}</script></head>"
        f'<body><svg><path d="{"M0 0 " * 500}"/></svg>{body}'
        f'<button data-testid="buy-button" onclick="buy()">Buy tickets</button>'
        f"<script>{script}</script></body></html>"
    )


def test_large_page_shrinks_dramatically():
    original = big_page()
    cleaned = clean_html(original)

    assert len(original) > 600_000
    # ARCHITECTURE.md §7 targets ~800 KB down to under 40 KB. The invariant
    # worth asserting is the ratio and the ceiling, not a size that only
    # reflects how this fixture was written.
    assert len(cleaned) < len(original) * 0.10
    assert len(cleaned) < 40_000


def test_scripts_styles_and_svg_are_removed_entirely():
    cleaned = clean_html(big_page(rows=2))

    assert "<script" not in cleaned
    assert "<style" not in cleaned
    assert "<svg" not in cleaned
    assert "var x" not in cleaned


def test_inline_event_handlers_are_stripped():
    cleaned = clean_html('<button onclick="buy()" onmouseover="x()">Buy</button>')

    assert "onclick" not in cleaned
    assert "onmouseover" not in cleaned
    assert "Buy" in cleaned


def test_selectable_attributes_survive():
    cleaned = clean_html(
        '<button id="go" class="primary" role="button" type="submit" '
        'data-testid="buy-button" aria-label="Buy tickets" '
        'style="color:red" data-analytics="42">Buy</button>'
    )

    for kept in ("id=", "class=", "role=", "type=", "data-testid=", "aria-label="):
        assert kept in cleaned
    assert "style=" not in cleaned
    assert "data-analytics" not in cleaned


def test_text_content_is_preserved_because_it_names_the_control():
    cleaned = clean_html("<div><button>Get tickets</button></div>")

    assert "Get tickets" in cleaned


def test_comments_are_dropped():
    cleaned = clean_html("<div><!-- build 7fa3c2, 40kb of feature flags --><b>x</b></div>")

    assert "build 7fa3c2" not in cleaned


def test_byte_ceiling_is_enforced():
    cleaned = clean_html(big_page(), max_bytes=2_000)

    assert len(cleaned.encode("utf-8")) <= 2_000


def test_empty_input_is_not_an_error():
    assert clean_html("") == ""
