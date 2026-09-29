"""
report_layout.py

A MECHANICAL CHECK ON A RENDERED PAGE: no laid-out box may extend past
the page's content edges. WeasyPrint exposes every box it drew with its
position and width, which is exactly the geometry the PDF was made from,
so a table that outgrew the measure -- a run of `white-space: nowrap`
cells, a flex item wider than its half -- is a fact the test can state
rather than something a reader notices on paper.

    overflowing_boxes(document, tolerance_pt=0.5) -> [(page, tag, class, left, right, overrun), ...]

Branch 7's review caught the severe-weather table sitting 51 pt past the
right margin and the monthly table 5 pt past it; this is the check that
would have caught both before a render was looked at. test_site_report.py
and test_landform_section.py run it over every page they render.

    captions_apart(document) -> [(block pages, caption pages, caption text), ...]

THE SAME BOX TREE SAYS WHERE A CAPTION FELL. Branch 21 bound every
caption to the block it describes (components/caption.html); this is
the check that the binding holds on the page: a caption on ONE page,
the page its block ENDS on -- the block may start earlier, the caption
may not be left behind or carried over alone.
"""


def _walk(box):
    yield box
    for child in getattr(box, "children", []) or []:
        yield from _walk(child)


def overflowing_boxes(document, tolerance_pt: float = 0.5) -> list:
    """Every box on every page whose border box crosses the page's content
    box by more than `tolerance_pt` on the left or the right. The page's
    margin boxes (the running footer) are not part of the page box tree
    and are not measured; everything the sections draw is."""
    overruns = []
    for number, page in enumerate(document.pages, start=1):
        page_box = page._page_box
        left = page_box.content_box_x()
        right = left + page_box.width
        for box in _walk(page_box):
            element = getattr(box, "element", None)
            if element is None or not hasattr(box, "position_x") or not hasattr(box, "width"):
                continue
            if type(box).__name__ in ("PageBox", "MarginBox"):
                continue
            box_left = box.position_x
            box_right = box.position_x + box.width
            overrun = max(box_right - right, left - box_left)
            if overrun > tolerance_pt:
                overruns.append(
                    (number, element.tag, element.get("class") or "", round(box_left, 2), round(box_right, 2), round(overrun, 2))
                )
    return overruns


# The captions bound to a block: inside a .captioned wrapper, or under a
# block that is meant to split and binds only its edge (report.css).
_EDGE_BOUND = ("back-matter__vintage", "record-step")


def _classes(element) -> list:
    return (element.get("class") or "").split()


def captions_apart(document) -> list:
    """Every bound caption that is split across pages, or that does not sit
    on the last page of the block before it, as (block pages, caption
    pages, the caption's first words). Empty when every caption is with
    its block."""
    pages = {}
    root = None
    for number, page in enumerate(document.pages, start=1):
        for box in _walk(page._page_box):
            element = getattr(box, "element", None)
            if element is None:
                continue
            if root is None or element.tag == "html":
                root = element if element.tag == "html" else root
            pages.setdefault(element, set()).add(number)
    parent_of = {child: parent for parent in root.iter() for child in parent} if root is not None else {}
    apart = []
    for element, on in pages.items():
        if element.tag != "p" or "caption" not in _classes(element) or element not in parent_of:
            continue
        parent = parent_of[element]
        if "captioned" not in _classes(parent) and not any(c in _classes(parent) for c in _EDGE_BOUND):
            continue
        siblings = list(parent)
        index = siblings.index(element)
        if index == 0:
            continue
        block = siblings[index - 1]
        # A block that drew no box of its own is judged by its descendants.
        block_pages = set().union(*(pages.get(e, set()) for e in block.iter()))
        if len(on) > 1 or not block_pages or min(on) != max(block_pages):
            apart.append((sorted(block_pages), sorted(on), " ".join("".join(element.itertext()).split())[:60]))
    return apart
