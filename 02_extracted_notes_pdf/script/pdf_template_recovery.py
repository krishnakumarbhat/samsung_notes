"""Reconstruct embedded-PDF paper for legacy Samsung Notes exports.

Legacy exports either drop the paper of ``custom PDF page N`` templates or
append ink overlays as extra pages.  This module rebuilds the visible page
sequence by splicing the *original* vector pages of the embedded document(s)
in the exact visible order, preserving non-PDF pages through the CLI's own
render.  Nothing is guessed; every binding is proved before use:

* Visible order comes from ``pageIdInfo.dat`` via ``page_delta`` (fail-closed
  on missing, duplicate or unlisted page records).
* A *single* embedded document binds directly: ``custom PDF page N`` is page
  ``N`` of that document, and the reference must be in range while the page
  aspect cross-checks the CLI's reported height — in the source page's upright
  MediaBox orientation or its quarter-turned presentation (pinned/rotated
  paper), never by a free-form size guess.
* *Multiple* embedded documents bind only through contiguous runs that reset
  numbering (``1..n``) and match document page counts uniquely.  Shared counts
  or unmatched runs fail closed with ``UnsupportedRecovery``; filename order
  is never assumed.  Runs that start partway through a document — because
  pages were removed from the note — or that split at a numbering gap are
  never bound by their length; they bind only when the page geometry proves
  exactly one assignment, otherwise recovery stays fail-closed.
* Every custom-PDF page is rendered once by the CLI and its content stream is
  proved to be exactly the deterministic page background (one full-page fill
  in the reported background colour, no other paint operator).  Ink, typed
  text or images over embedded paper are composed instead as a *transparent
  vector ink layer*: the CLI page background is removed, the ink colours are
  re-tuned for a white canvas, and the layer is drawn over the pristine
  embedded page.  The composition fails closed when the background cannot be
  removed as a single full-page fill.
* Non-PDF pages keep the CLI's own rendered page (background and template as
  the renderer supports them), so blank built-in pages stay at their visible
  position; a dark canvas on such a page is repainted on white so the ink
  stays readable.

The source tree, note database and VM are read-only; all output is written
under the caller-provided ``work`` directory.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Sequence

__all__ = ["recover", "UnsupportedRecovery"]

# CLI PDF output is 96 dpi, so note units convert with a factor of 0.75.
_UNIT_SCALE = 0.75
_CUSTOM_TEMPLATE = re.compile(r"custom PDF page (\d+)$")


class UnsupportedRecovery(RuntimeError):
    """The note cannot be reconstructed safely; no output was produced."""


def _ie():
    """Import the existing exporter lazily (shared CLI and verify helpers)."""
    import importlib

    try:
        return importlib.import_module("incremental_export")
    except ModuleNotFoundError:
        return importlib.import_module("02_extracted_notes_pdf.script.incremental_export")


def _pd():
    """Import the page-delta module for authoritative manifest ordering."""
    import importlib

    try:
        return importlib.import_module("page_delta")
    except ModuleNotFoundError:
        return importlib.import_module("02_extracted_notes_pdf.script.page_delta")


def _build_archive(source: Path, work: Path) -> Path:
    """Delegate archive construction so it stays byte-identical to the exporter."""
    return _pd()._build_archive(Path(source), Path(work))


def _color():
    """Import the light-canvas transformer lazily."""
    import importlib

    try:
        return importlib.import_module("pdf_color_normalize")
    except ModuleNotFoundError:
        return importlib.import_module("02_extracted_notes_pdf.script.pdf_color_normalize")


def _fitz():
    """Import the project's PyMuPDF compatibility module lazily."""
    import importlib

    for name in ("pymupdf_compat", "fitz"):
        try:
            module = importlib.import_module(name)
            return getattr(module, "fitz", module)
        except ModuleNotFoundError:
            continue
    raise UnsupportedRecovery("PyMuPDF is unavailable for transparent overlay composition")


def _normalize_cli_page(page) -> int:
    """Apply the light-canvas repair to a CLI-rendered page, in place.

    The renderer paints the note's own page background; on a dark-theme note
    that background hides the ink.  Only the page background and the ink
    colours are touched, so pasted images and templates stay untouched.
    """
    import pikepdf

    box = page.mediabox
    page_size = (float(box[2]) - float(box[0]), float(box[3]) - float(box[1]))
    contents = page.obj.get("/Contents")
    streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
    changed = 0
    for stream in streams:
        if stream is None:
            continue
        data = stream.read_bytes()
        normalized, facts = _color().normalize_content_stream(data, page_size=page_size)
        changed += int(facts.get("background_fills", 0))
        if normalized != data:
            stream.write(normalized)
    return changed


def _ink_streams(page, page_dict: dict, work: Path) -> Path:
    """Write a one-page PDF containing only a transparent proof of the ink."""
    import pikepdf

    output = work / f"ink-{page_dict['index']:06d}.pdf"
    with pikepdf.Pdf.new() as ink:
        ink.pages.extend([page])
        target = ink.pages[0]
        box = target.mediabox
        page_size = (float(box[2]) - float(box[0]), float(box[3]) - float(box[1]))
        contents = target.obj.get("/Contents")
        streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
        removed = 0
        for stream in streams:
            if stream is None:
                continue
            data = stream.read_bytes()
            stripped, facts = _color().normalize_content_stream(
                data, strip_background=True, reverse_colors=True, page_size=page_size,
            )
            removed += int(facts.get("background_fills", 0))
            if stripped != data:
                stream.write(stripped)
        if removed != 1:
            raise UnsupportedRecovery(
                f"visible page {page_dict['index']} has {removed} removable "
                "full-page background fill(s); transparent ink composition is "
                "not provable",
            )
        ink.save(output)
    return output


def _collapse_warnings(warnings: Sequence[str]) -> list[str]:
    """Fold repeated per-page warning tags into one counted line."""
    groups: dict[str, list[str]] = {}
    for warning in warnings:
        match = re.match(r"Warning \[([^\]]+)\]", warning)
        groups.setdefault(match.group(1) if match else warning, []).append(warning)
    collapsed: list[str] = []
    for key, items in groups.items():
        if len(items) > 2:
            collapsed.append(f"Warning [{key}] on {len(items)} pages")
        else:
            collapsed.extend(dict.fromkeys(items))
    return collapsed


def _probe(archive: Path, work: Path) -> tuple[int, list[dict], list[str]]:
    """Parse the CLI's authoritative per-page template inventory.

    Returns ``(visible_count, pages, warnings)``; each page dict carries
    ``index`` (0-based visible order), ``width``/``height`` (note units),
    ``background`` (``#rrggbb``), ``template`` and ``strokes``.
    """
    ie = _ie()
    out = ie.execute(
        [str(ie.CLI), str(archive), "--pages", "1", "--verify-integrity",
         "-o", str(work / "probe.pdf")],
        600,
    )
    match = re.search(r"(\d+) visible page\(s\), (\d+) stored page record\(s\)", out)
    if not match or int(match[1]) <= 0:
        raise UnsupportedRecovery("no authoritative visible page count")
    visible, stored = int(match[1]), int(match[2])
    if visible > stored:
        raise UnsupportedRecovery(
            f"the renderer reports {visible} visible page(s) but only {stored} "
            "stored page record(s)",
        )
    integrity = re.findall(
        r"Integrity (\w+): (\d+) matched, (\d+) mismatched, (\d+) unavailable", out,
    )
    for label, _matched, mismatched, unavailable in integrity:
        if int(mismatched) or int(unavailable):
            raise UnsupportedRecovery(
                f"source integrity is not fully verified ({label}: "
                f"{mismatched} mismatched, {unavailable} unavailable)",
            )
    if not integrity:
        raise UnsupportedRecovery("source integrity is not fully verified")
    pages: list[dict] = []
    for line in re.finditer(
        r"Page (\d+): (\d+) x (\d+), background #(\w+), template ([^,]+), (\d+) strokes",
        out,
    ):
        pages.append(
            {"index": int(line.group(1)), "width": int(line.group(2)),
             "height": int(line.group(3)), "background": "#" + line.group(4),
             "template": line.group(5).strip(), "strokes": int(line.group(6))},
        )
    if len(pages) != int(match[1]) or [p["index"] for p in pages] != list(range(int(match[1]))):
        raise UnsupportedRecovery("per-page inventory is incomplete")
    warnings = sorted(set(re.findall(r"Warning \[[^\]]+\][^\n]*", out)))
    if visible < stored:
        warnings.append(
            f"pdf-template-recovery: {stored - visible} stored page record(s) "
            "are not visible and are not reconstructed",
        )
    return int(match[1]), pages, _collapse_warnings(warnings)


def _render_all(archive: Path, out_pdf: Path) -> int:
    """Render every visible page once with the CLI; return the page count."""
    ie = _ie()
    ie.execute([str(ie.CLI), str(archive), "-f", "pdf", "-o", str(out_pdf)], 900)
    return ie.verify(out_pdf)


def _embedded_pdfs(source: Path) -> list[Path]:
    """Source-document PDFs bundled under media/ (read-only)."""
    return _ie().embedded_pdfs(Path(source))


def _pdf_page_sizes(pdfs: Sequence[Path]) -> dict[str, list[tuple[float, float]]]:
    """Read every embedded PDF page's MediaBox without modifying the file."""
    import pikepdf

    sizes: dict[str, list[tuple[float, float]]] = {}
    for path in pdfs:
        dims: list[tuple[float, float]] = []
        with pikepdf.open(path) as pdf:
            for page in pdf.pages:
                box = page.mediabox
                width = float(box[2]) - float(box[0])
                height = float(box[3]) - float(box[1])
                if width <= 0 or height <= 0:
                    raise UnsupportedRecovery(f"{path.name}: invalid page dimensions")
                dims.append((width, height))
        if not dims:
            raise UnsupportedRecovery(f"{path.name}: document has no pages")
        sizes[path.name] = dims
    return sizes


def _aspect_matches(box: tuple[float, float], page: dict) -> bool:
    """Whether a visible page's reported size matches the source page geometry.

    The note presents embedded paper either upright (its MediaBox orientation)
    or turned a quarter turn, which is how a pinned/rotated page is stored, so
    the reported height is checked against both orientations of the source
    aspect with the tolerance of the upright check.  Anything else stays a
    mismatch; the orientation that matches is a geometry proof of the same
    source page, not a size guess.
    """
    width, height = float(page["width"]), float(page["height"])
    tolerance = max(4.0, 0.003 * height)
    return any(
        abs(box_h * width / box_w - height) <= tolerance
        for box_w, box_h in (box, (box[1], box[0]))
    )


def _check_height(name: str, number: int, box: tuple[float, float], page: dict) -> None:
    """Cross-check the CLI's reported page height against the embedded aspect.

    Observed scaled-height deviations stay below 2.2 note units; the tolerance
    keeps rounding harmless while still rejecting a wrong binding.  A page
    whose geometry matches neither presentation of the referenced source page
    fails closed and reports the upright scaled height.
    """
    if _aspect_matches(box, page):
        return
    scaled = box[1] * float(page["width"]) / box[0]
    raise UnsupportedRecovery(
        f"visible page {page['index']} height {page['height']} disagrees with "
        f"{name} page {number} (scaled {scaled:.1f})",
    )


def _runs(pages: Sequence[dict]) -> list[list[tuple[int, int]]]:
    """Split custom-PDF pages into contiguous runs with resetting numbering.

    A run is a maximal contiguous visible sequence whose template numbers
    increase by one.  Non-custom pages break a run, and so does any numbering
    gap or reset: the following page then starts a new run at its own number,
    which may lie partway through a document because pages were removed from
    the note.  Such partial runs are never bound by their length;
    ``_bind_pdfs`` accepts them only through a unique page-geometry proof and
    fails closed when that proof is missing or ambiguous.
    """
    runs: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    for page in pages:
        match = _CUSTOM_TEMPLATE.match(page["template"])
        if not match:
            if current:
                runs.append(current)
                current = []
            continue
        number = int(match.group(1))
        if number < 1:
            raise UnsupportedRecovery("custom PDF page numbers must be positive")
        if current and number != current[-1][1] + 1:
            runs.append(current)
            current = []
        current.append((page["index"], number))
    if current:
        runs.append(current)
    return runs


def _bind_single(pages: Sequence[dict],
                 sizes: dict[str, list[tuple[float, float]]]) -> dict[int, tuple[str, int]]:
    """Bind one embedded document directly by its reported page number."""
    name, dims = next(iter(sizes.items()))
    binding: dict[int, tuple[str, int]] = {}
    for page in pages:
        match = _CUSTOM_TEMPLATE.match(page["template"])
        if not match:
            continue
        number = int(match.group(1))
        if not 1 <= number <= len(dims):
            raise UnsupportedRecovery(
                f"visible page {page['index']} references {name} page {number}, "
                f"but the document has {len(dims)} page(s)",
            )
        _check_height(name, number, dims[number - 1], page)
        binding[page["index"]] = (name, number - 1)
    if not binding:
        raise UnsupportedRecovery("no custom-PDF template pages to recover")
    return binding


def _bind_runs(runs: Sequence[Sequence[tuple[int, int]]],
               sizes: dict[str, list[tuple[float, float]]],
               pages: Sequence[dict]) -> dict[int, tuple[str, int]]:
    """Bind several embedded documents through count-unique run matching.

    Each contiguous run must match exactly one still-unused document whose
    page count equals the run length; ties (shared counts) and gaps (no
    matching document) fail closed rather than assuming filename order.
    """
    by_count: dict[int, list[str]] = {}
    for name, dims in sizes.items():
        by_count.setdefault(len(dims), []).append(name)
    available = {name for names in by_count.values() for name in names}
    binding: dict[int, tuple[str, int]] = {}
    for run in runs:
        if [number for _, number in run] != list(range(1, len(run) + 1)):
            raise UnsupportedRecovery(
                "partial template run requires a unique page-geometry binding",
            )
        candidates = [n for n in by_count.get(len(run), []) if n in available]
        if len(candidates) != 1:
            raise UnsupportedRecovery(
                f"template run of {len(run)} page(s) at visible page {run[0][0]} has "
                f"{len(candidates)} matching embedded document(s); source binding is "
                "ambiguous",
            )
        name = candidates[0]
        available.discard(name)
        dims = sizes[name]
        for index, number in run:
            _check_height(name, number, dims[number - 1], pages[index])
            binding[index] = (name, number - 1)
    return binding


def _geometry_matches(dims: Sequence[tuple[float, float]], pages: Sequence[dict]) -> bool:
    """Whether every referenced page of a document matches the visible geometry.

    The check mirrors :func:`_check_height` but never raises: it is used to
    decide which embedded document a visible page sequence can refer to.
    """
    for page in pages:
        match = _CUSTOM_TEMPLATE.match(page["template"])
        if not match:
            continue
        number = int(match.group(1))
        if not 1 <= number <= len(dims):
            return False
        if not _aspect_matches(dims[number - 1], page):
            return False
    return True


def _bind_runs_by_geometry(
    runs: Sequence[Sequence[tuple[int, int]]],
    sizes: dict[str, list[tuple[float, float]]],
    pages: Sequence[dict],
) -> dict[int, tuple[str, int]]:
    """Bind page runs to embedded documents through the page geometry.

    A note may attach the same document more than once, or attach a document
    whose page count differs from the visible run, so the run lengths cannot
    decide the binding.  The geometry can: every referenced page number must
    exist and the visible height must match the embedded aspect.  One attached
    document may back several runs (each visible page references its own source
    page), so every run may choose any geometrically compatible document; the
    full assignment must be unique, otherwise recovery stays ambiguous.  A
    document is deliberately *not* consumed by a run: excluding an already
    used document from later runs would turn an unproven assignment into a
    silent, potentially wrong binding.
    """
    by_index = {page["index"]: page for page in pages}
    candidates: list[list[str]] = []
    for run in runs:
        run_pages = [by_index[index] for index, _ in run]
        options = [
            name for name, dims in sizes.items()
            if _geometry_matches(dims, run_pages)
        ]
        if not options:
            raise UnsupportedRecovery(
                f"no embedded document matches the geometry of the page run at "
                f"visible page {run[0][0]}",
            )
        candidates.append(options)
    solutions: list[list[str]] = []

    def _assign(index: int, choice: list[str]) -> None:
        if len(solutions) > 1:
            return
        if index == len(runs):
            solutions.append(list(choice))
            return
        for name in candidates[index]:
            choice.append(name)
            _assign(index + 1, choice)
            choice.pop()

    _assign(0, [])
    if len(solutions) != 1:
        raise UnsupportedRecovery(
            f"{len(solutions)} embedded document assignment(s) match the visible "
            "page geometry; the source binding stays ambiguous",
        )
    assignment = solutions[0]
    binding: dict[int, tuple[str, int]] = {}
    for run, name in zip(runs, assignment):
        dims = sizes[name]
        for index, number in run:
            binding[index] = (name, number - 1)
    return binding


def _bind_pdfs(pages: Sequence[dict],
               sizes: dict[str, list[tuple[float, float]]]) -> dict[int, tuple[str, int]]:
    """Bind every visible custom-PDF page to an exact embedded page.

    One embedded document binds by direct page reference; several documents
    bind only via contiguous resetting runs matched uniquely by page count.
    When the runs cannot decide the binding — shared counts, a run that starts
    partway through a document, or a numbering gap that split a run — the page
    geometry may still prove exactly one assignment, and only that proof is
    used; anything ambiguous fails closed.
    """
    if len(sizes) == 1:
        return _bind_single(pages, sizes)
    runs = _runs(pages)
    if not runs:
        raise UnsupportedRecovery("no custom-PDF template pages to recover")
    try:
        return _bind_runs(runs, sizes, pages)
    except UnsupportedRecovery:
        binding = _bind_runs_by_geometry(runs, sizes, pages)
        if binding:
            return binding
        raise


def _content_stream(pdf, index: int) -> str:
    """Decoded content-stream text of one page of an open pikepdf document."""
    import pikepdf

    contents = pdf.pages[index].obj.get("/Contents")
    if contents is None:
        return ""
    items = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
    return b"".join(item.read_bytes() for item in items).decode("latin-1")


def _assert_background_only(content: str, page: dict) -> None:
    """Prove a rendered custom-PDF page carries nothing but its background.

    The renderer emits the page clip, one full-page rectangle filled with the
    reported page background, and a closing clip.  Any other operator (ink,
    typed text, images, shadings, ExtGState) means the bare embedded page
    would silently lose content, so recovery fails closed instead.
    """
    tokens = re.findall(r'/[^\s/[\]<>()]+|[-+]?[\d.]+|[A-Za-z*\'"]+', content)
    allowed = {"q", "Q", "m", "l", "h", "W", "n", "cm", "g", "rg", "f", "f*"}
    fills = 0
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if token[0].isalpha() or token in ("f*", "'", '"'):
            if token not in allowed:
                raise UnsupportedRecovery(
                    f"visible page {page['index']} has non-background content "
                    f"operator {token!r}",
                )
            if token.startswith("f"):
                fills += 1
    if fills != 1:
        raise UnsupportedRecovery(
            f"visible page {page['index']} background fill count is {fills}",
        )
    flat = re.sub(r"\s+", " ", content)
    color = re.search(r"([\d.]+)(?: ([\d.]+) ([\d.]+))? (?:g|rg)\b", flat)
    if not color:
        raise UnsupportedRecovery(
            f"visible page {page['index']} background color is unreadable",
        )
    components = [float(color.group(i)) for i in (1, 2, 3) if color.group(i)]
    if len(components) == 1:
        components *= 3
    hex_value = page["background"].lstrip("#")
    expected = [int(hex_value[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    if any(abs(a - b) > 1e-3 for a, b in zip(components, expected)):
        raise UnsupportedRecovery(
            f"visible page {page['index']} fill color does not match "
            f"{page['background']}",
        )
    rect = re.search(r"0 0 m ([\d.]+) 0 l \1 ([\d.]+) l 0 \2 l", flat)
    if not rect:
        raise UnsupportedRecovery(
            f"visible page {page['index']} background is not a full-page rectangle",
        )
    rect_w, rect_h = float(rect.group(1)), float(rect.group(2))
    candidates = [(page["width"], page["height"]),
                  (page["width"] * _UNIT_SCALE, page["height"] * _UNIT_SCALE)]
    if not any(abs(rect_w - w) <= 0.01 and abs(rect_h - h) <= 0.01
               for w, h in candidates):
        raise UnsupportedRecovery(
            f"visible page {page['index']} background rectangle size mismatch",
        )


def _preserve_navigation(output: Any, sources: dict[str, Any],
                         binding: dict[int, tuple[str, int]]) -> None:
    """Remap embedded bookmarks and named destinations to the assembled pages.

    Copying page objects alone drops catalog-level navigation. Only destinations
    whose source page has a unique copy are transferable: omitted pages have no
    valid target, while duplicate copies cannot be disambiguated safely.
    """
    import pikepdf

    all_names: dict[str, object] = {}
    all_legacy: dict[str, object] = {}
    with output.open_outline() as destination_outline:
        for name, source in sources.items():
            mapped: dict[tuple[int, int], list[int]] = {}
            for index, (bound_name, page_number) in binding.items():
                if bound_name == name:
                    mapped.setdefault(source.pages[page_number].obj.objgen, []).append(index)
            source_names = source.Root.get("/Names", {}).get("/Dests")
            names = dict(pikepdf.NameTree(source_names).items()) if source_names is not None else {}
            legacy = source.Root.get("/Dests", {})

            def remap(value: Any) -> Any:
                """Resolve a source destination and retain its exact PDF viewport."""
                if isinstance(value, pikepdf.String):
                    value = names.get(str(value))
                elif isinstance(value, pikepdf.Name):
                    value = legacy.get(str(value))
                if isinstance(value, pikepdf.Dictionary):
                    value = value.get("/D")
                if not isinstance(value, pikepdf.Array) or len(value) < 2:
                    raise UnsupportedRecovery(f"{name}: unsupported or unresolved PDF destination")
                target = value[0]
                objgen = getattr(target, "objgen", None)
                if objgen is None:
                    # pikepdf surfaces a PDF null target as None: the
                    # destination names no page, so it has no provable target
                    # and is dropped exactly like an omitted source page.
                    return None
                choices = mapped.get(objgen, [])
                if not choices:
                    return None
                if len(choices) != 1:
                    raise UnsupportedRecovery(f"{name}: navigation target has multiple visible copies")
                # The remaining entries are a PDF fit-name and scalar coordinates.
                return pikepdf.Array([output.pages[choices[0]].obj, *list(value)[1:]])

            def copy_items(items: Sequence[Any]) -> list[Any]:
                """Keep outline hierarchy and style without foreign page references."""
                copied = []
                for item in items:
                    children = copy_items(item.children)
                    target = item.destination
                    action = item.action
                    if action is not None:
                        if str(action.get("/S")) != "/GoTo":
                            raise UnsupportedRecovery(f"{name}: unsupported outline action")
                        target = action.get("/D")
                    destination = remap(target) if target is not None else None
                    if destination is None and not children:
                        continue
                    obj = output.make_indirect(pikepdf.Dictionary())
                    if item.obj is not None:
                        if "/C" in item.obj:
                            obj.C = pikepdf.Array(list(item.obj.C))
                        if "/F" in item.obj:
                            obj.F = int(item.obj.F)
                    copied_item = pikepdf.OutlineItem(item.title, destination=destination, obj=obj)
                    copied_item.is_closed = item.is_closed
                    copied_item.children = children
                    copied.append(copied_item)
                return copied

            with source.open_outline() as outline:
                destination_outline.root.extend(copy_items(outline.root))
            for key, value in names.items():
                destination = remap(value)
                if destination is not None:
                    if key in all_names:
                        raise UnsupportedRecovery(f"duplicate named destination: {key}")
                    all_names[key] = destination
            for key, value in legacy.items():
                destination = remap(value)
                if destination is not None:
                    if str(key) in all_legacy:
                        raise UnsupportedRecovery(f"duplicate legacy destination: {key}")
                    all_legacy[str(key)] = destination
    if all_names:
        entries = []
        for key in sorted(all_names):
            entries.extend([pikepdf.String(key), all_names[key]])
        output.Root.Names = pikepdf.Dictionary(
            Dests=output.make_indirect(pikepdf.Dictionary(Names=pikepdf.Array(entries))))
    if all_legacy:
        output.Root.Dests = pikepdf.Dictionary(all_legacy)


def recover(source: Path, work: Path, *, allow_ink_overlay: bool = False) -> tuple[Path, int, list[str]]:
    """Reconstruct a note whose paper is one or more embedded PDFs.

    Returns ``(output_pdf, visible_page_count, warnings)``.  Raises
    ``UnsupportedRecovery`` — producing no output — whenever the source
    binding is ambiguous.  With ``allow_ink_overlay=True``, a custom-PDF page
    carrying strokes is composed as a transparent vector ink layer over the
    pristine embedded page; the page background must be a single removable
    full-page fill or recovery fails closed.
    """
    import pikepdf

    source, work = Path(source), Path(work)
    work.mkdir(parents=True, exist_ok=True)
    archive = _build_archive(source, work)
    count, pages, warnings = _probe(archive, work)
    pd = _pd()
    try:
        order = pd._ordered_visible_pages(archive, count, pages)
    except Exception as exc:  # fail closed on any manifest mapping problem
        raise UnsupportedRecovery(f"page manifest mapping failed: {exc}") from exc
    if len(order) != count:
        raise UnsupportedRecovery("manifest order does not cover every visible page")
    pdfs = _embedded_pdfs(source)
    if not pdfs:
        raise UnsupportedRecovery("no embedded PDF document under media/")
    overlay_indices: set[int] = set()
    for page in pages:
        if page["strokes"] and _CUSTOM_TEMPLATE.match(page["template"]):
            if not allow_ink_overlay:
                raise UnsupportedRecovery(
                    f"visible page {page['index']} carries {page['strokes']} stroke(s) "
                    "over embedded PDF paper; retry with allow_ink_overlay=True "
                    "for a proved transparent composition",
                )
            overlay_indices.add(page["index"])
    binding = _bind_pdfs(pages, _pdf_page_sizes(pdfs))
    import json

    manifest = {
        "pages": count,
        "binding": {str(index): [name, number] for index, (name, number) in binding.items()},
        "sources": {path.name: str(path) for path in pdfs},
        "cli_pages": [index for index in range(count) if index not in binding],
    }
    (work / "recovery-binding.json").write_text(json.dumps(manifest, indent=2))
    rendered = work / "cli-render.pdf"
    cli_count = _render_all(archive, rendered)
    if cli_count != count:
        raise UnsupportedRecovery(f"CLI rendered {cli_count} pages, expected {count}")
    sources: dict[str, object] = {}
    output = work / "recovered.pdf"
    ink_paths: dict[int, Path] = {}
    try:
        with pikepdf.open(rendered) as cli_pdf, pikepdf.Pdf.new() as out:
            for path in pdfs:
                sources[path.name] = pikepdf.open(path)
            for index, page in enumerate(pages):
                if index in binding:
                    name, number = binding[index]
                    if index not in overlay_indices:
                        try:
                            _assert_background_only(_content_stream(cli_pdf, index), page)
                        except UnsupportedRecovery:
                            # A zero stroke count does not prove there is no
                            # pasted image or typed/vector content. Preserve it
                            # with the same checked transparent composition.
                            if not allow_ink_overlay:
                                raise
                            overlay_indices.add(index)
                    out.pages.extend([sources[name].pages[number]])
                    if index in overlay_indices:
                        ink_paths[index] = _ink_streams(cli_pdf.pages[index], page, work)
                else:
                    # Non-PDF page: keep the CLI's own render so template and
                    # background stay exactly as the renderer composes them,
                    # but repair a dark canvas so the ink stays readable.
                    _normalize_cli_page(cli_pdf.pages[index])
                    out.pages.extend([cli_pdf.pages[index]])
            if len(out.pages) != count:
                raise UnsupportedRecovery("assembled page count mismatch")
            for index, (name, number) in sorted(binding.items()):
                try:
                    want = [float(v) for v in sources[name].pages[number].mediabox]
                    got = [float(v) for v in out.pages[index].mediabox]
                except Exception as exc:
                    raise UnsupportedRecovery(
                        f"visible page {index} lost its page dimensions: {exc}",
                    ) from exc
                if want != got:
                    raise UnsupportedRecovery(
                        f"visible page {index} dimensions were not preserved",
                    )
            _preserve_navigation(out, sources, binding)
            out.save(output, compress_streams=True,
                     object_stream_mode=pikepdf.ObjectStreamMode.generate)
    finally:
        for src in sources.values():
            src.close()
    if ink_paths:
        fitz = _fitz()
        document = fitz.open(output)
        try:
            for index, ink_path in sorted(ink_paths.items()):
                ink = fitz.open(ink_path)
                try:
                    page = document[index]
                    before_text = len(page.get_text())
                    page.show_pdf_page(page.rect, ink, 0, overlay=True)
                    after_text = len(page.get_text())
                    if after_text + 3 < before_text:
                        raise UnsupportedRecovery(
                            f"visible page {index} lost text during ink overlay "
                            f"({before_text} -> {after_text})",
                        )
                finally:
                    ink.close()
            overlaid = work / "recovered-overlay.pdf"
            document.save(overlaid, garbage=3, deflate=True)
        finally:
            document.close()
        output = overlaid
        warnings.append(
            f"pdf-template-recovery: transparent ink overlay on "
            f"{len(ink_paths)} embedded PDF page(s)",
        )
    _ie().verify(output, count)
    if binding:
        try:
            _ie().content_pages(output, count)
        except Exception as exc:  # blank paper is faithful, report honestly
            warnings.append(f"pdf-template-recovery: raster ink probe skipped ({exc})")
    warnings.append(
        f"pdf-template-recovery: spliced {len(binding)} embedded PDF page(s), "
        f"{count - len(binding)} page(s) from the CLI render",
    )
    return output, count, warnings


def main(argv: list[str] | None = None) -> int:
    """CLI: recover one note directory into a work directory (JSON on stdout)."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("work", type=Path)
    args = parser.parse_args(argv)
    try:
        output, count, warnings = recover(args.source, args.work)
    except UnsupportedRecovery as exc:
        print(json.dumps({"status": "unsupported", "reason": str(exc)}))
        return 3
    print(json.dumps({"status": "recovered", "path": str(output),
                      "pages": count, "warnings": warnings}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
