"""Fail-closed checks for embedded-PDF paper reconstruction.

Synthetic fixtures exercise the binding rules without the CLI (probe and
render seams are patched); real-sample tests run the full pipeline when the
stage2 snapshots are present and can be disabled with
``PDF_TEMPLATE_RECOVERY_REAL=0``.
"""
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pdf_template_recovery as ptr

try:
    import pikepdf
except ImportError:  # pragma: no cover - environment without pikepdf
    pikepdf = None

SAMPLES = Path(os.environ.get(
    "PDF_TEMPLATE_RECOVERY_SAMPLES",
    str(Path(__file__).resolve().parent.parent / "ai_agend_history" / "stage2_source_samples"),
))
REAL = os.environ.get("PDF_TEMPLATE_RECOVERY_REAL", "1") == "1"

_UUIDS = [f"{1000 + i:04d}0000-1000-4000-8000-{100000000000 + i}" for i in range(64)]


def _paper(path: Path, sizes: list[tuple[float, float]]) -> None:
    """Vector fixture paper: every page a distinct solid rectangle."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.Pdf.new() as pdf:
        for i, (width, height) in enumerate(sizes):
            page = pdf.add_blank_page(page_size=(width, height))
            red = (0.15 + 0.3 * i) % 1.0
            body = (f"{red:.3f} 0.250 0.600 rg "
                    f"10 10 {width * 0.5:.2f} {height * 0.5:.2f} re f")
            page.obj.Contents = pdf.make_stream(body.encode("latin-1"))
    pdf.save(path)


def _manifest(root: Path, page_ids: list[str]) -> None:
    """Authoritative page manifest plus matching .page entries."""
    blob = b"\x00" * 32 + struct.pack("<H", len(page_ids))
    for page_id in page_ids:
        encoded = page_id.encode("utf-16-le")
        blob += struct.pack("<H", len(encoded) // 2) + encoded + b"\x11" * 32
    (root / "pageIdInfo.dat").write_bytes(blob)
    for page_id in page_ids:
        (root / f"{page_id}.page").write_bytes(b"\x00")


def _page(index: int, template: str, *, width: int = 1600, height: int = 2000,
          background: str = "#010101", strokes: int = 0) -> dict:
    return {"index": index, "width": width, "height": height,
            "background": background, "template": template, "strokes": strokes}


def _render(pages: list[dict], extras: dict[int, str] | None = None):
    """Fake CLI full render reproducing the deterministic background stream."""
    extras = extras or {}

    def render(_archive: Path, out_pdf: Path) -> int:
        with pikepdf.Pdf.new() as pdf:
            for page in pages:
                added = pdf.add_blank_page(
                    page_size=(page["width"] * 0.75, page["height"] * 0.75))
                gray = int(page["background"][1:3], 16) / 255.0
                clip = (f"q 0 {page['height'] * 0.75} m {page['width'] * 0.75} "
                        f"{page['height'] * 0.75} l {page['width'] * 0.75} 0 l 0 0 l")
                body = (f"{clip}\nh\nW\nn\n"
                        f"q 0.75 0 0 -0.75 0 {page['height'] * 0.75} cm {gray:.9f} g "
                        f"0 0 m {page['width']} 0 l {page['width']} {page['height']} l "
                        f"0 {page['height']} l\nh\nf\nQ\n"
                        f"{clip}\nh\nW\nn\nQ\nQ" + extras.get(page["index"], ""))
                added.obj.Contents = pdf.make_stream(body.encode("latin-1"))
        pdf.save(out_pdf)
        return len(pages)

    return render


def _raster(path: Path, page: int) -> tuple[tuple[int, int], bytes]:
    """Low-resolution raster of one page for pixel-level comparison."""
    from PIL import Image

    prefix = path.parent / f"_raster-{page}"
    ptr._ie().execute(["pdftoppm", "-r", "36", "-png", "-f", str(page),
                       "-l", str(page), str(path), str(prefix)], 120)
    found = sorted(prefix.parent.glob(f"{prefix.name}-*.png"))
    if not found:
        raise AssertionError(f"could not rasterize page {page} of {path}")
    with Image.open(found[0]) as image:
        rgb = image.convert("RGB")
        return rgb.size, rgb.tobytes()


def _mean(pixels: bytes) -> float:
    """Mean luminance of packed RGB bytes."""
    values = [pixels[i] for i in range(0, len(pixels), 3)]
    return sum(values) / max(1, len(values))


@unittest.skipUnless(pikepdf, "pikepdf is required")
class SyntheticRecoveryTests(unittest.TestCase):
    """Binding rules, page-count preservation and render equivalence."""

    def _recover(self, source: Path, work: Path, probe, render=None):
        with patch.object(ptr, "_probe", probe), \
                patch.object(ptr, "_render_all", render or _render(probe.pages)):
            return ptr.recover(source, work)

    def test_single_pdf_splice_preserves_order_counts_and_render(self):
        sizes = [(400.0, 500.0), (400.0, 600.0)]
        pages = [_page(0, "custom PDF page 1", height=2000),
                 _page(1, "built-in 9", height=2262),
                 _page(2, "custom PDF page 2", height=2400)]

        def probe(archive, work):
            return 3, pages, []

        probe.pages = pages
        with tempfile.TemporaryDirectory(prefix="pdfrec-") as tmp:
            root = Path(tmp)
            _paper(root / "media" / "0@pdf_x.pdf", sizes)
            _manifest(root, _UUIDS[:3])
            output, count, warnings = self._recover(root, root / "work", probe)
            self.assertEqual(count, 3)
            self.assertTrue(output.exists())
            with pikepdf.open(output) as pdf:
                self.assertEqual(len(pdf.pages), 3)
                self.assertEqual([float(v) for v in pdf.pages[0].mediabox],
                                 [0.0, 0.0, 400.0, 500.0])
                self.assertEqual([float(v) for v in pdf.pages[1].mediabox],
                                 [0.0, 0.0, 1200.0, 1696.5])
                self.assertEqual([float(v) for v in pdf.pages[2].mediabox],
                                 [0.0, 0.0, 400.0, 600.0])
            self.assertEqual(_raster(output, 1),
                             _raster(root / "media" / "0@pdf_x.pdf", 1))
            self.assertEqual(_raster(output, 3),
                             _raster(root / "media" / "0@pdf_x.pdf", 2))
            self.assertNotEqual(_raster(output, 1), _raster(output, 3))
            self.assertTrue(any("spliced 2 embedded PDF page(s)" in w for w in warnings))
            self.assertFalse(any("ink probe skipped" in w for w in warnings))

    def test_ambiguous_shared_counts_fail_closed(self):
        sizes = [(400.0, 500.0), (400.0, 600.0), (400.0, 700.0)]
        heights = [2000, 2400, 2800]
        pages = [_page(i, f"custom PDF page {i % 3 + 1}", height=heights[i % 3])
                 for i in range(6)]

        def probe(archive, work):
            return 6, pages, []

        probe.pages = pages
        with tempfile.TemporaryDirectory(prefix="pdfrec-") as tmp:
            root = Path(tmp)
            _paper(root / "media" / "0@pdf_a.pdf", sizes)
            _paper(root / "media" / "1@pdf_b.pdf", sizes)
            _manifest(root, _UUIDS[:6])
            with self.assertRaisesRegex(ptr.UnsupportedRecovery, "ambiguous"):
                self._recover(root, root / "work", probe)
            self.assertFalse((root / "work" / "recovered.pdf").exists())

    def test_multi_pdf_run_binds_by_page_geometry(self):
        """A run that matches no document count still binds when the geometry does."""
        pages = [_page(i, f"custom PDF page {i + 1}", height=h)
                 for i, h in enumerate([2000, 2400, 2800])]

        def probe(archive, work):
            return 3, pages, []

        probe.pages = pages
        with tempfile.TemporaryDirectory(prefix="pdfrec-") as tmp:
            root = Path(tmp)
            _paper(root / "media" / "0@pdf_a.pdf", [(400.0, 500.0), (400.0, 600.0)])
            _paper(root / "media" / "1@pdf_b.pdf",
                   [(400.0, 500.0), (400.0, 600.0), (400.0, 700.0),
                    (400.0, 800.0)])
            _manifest(root, _UUIDS[:3])
            output, count, warnings = self._recover(root, root / "work", probe)
            self.assertEqual(count, 3)
            with pikepdf.open(output) as pdf:
                self.assertEqual([float(v) for v in pdf.pages[0].mediabox],
                                 [0.0, 0.0, 400.0, 500.0])
                self.assertEqual([float(v) for v in pdf.pages[2].mediabox],
                                 [0.0, 0.0, 400.0, 700.0])

    def test_ambiguous_page_geometry_fails_closed(self):
        """Two documents with the same geometry keep the binding ambiguous."""
        pages = [_page(i, f"custom PDF page {i + 1}", height=h)
                 for i, h in enumerate([2000, 2400, 2800])]

        def probe(archive, work):
            return 3, pages, []

        probe.pages = pages
        with tempfile.TemporaryDirectory(prefix="pdfrec-") as tmp:
            root = Path(tmp)
            _paper(root / "media" / "0@pdf_a.pdf",
                   [(400.0, 500.0), (400.0, 600.0), (400.0, 700.0)])
            _paper(root / "media" / "1@pdf_b.pdf",
                   [(400.0, 500.0), (400.0, 600.0), (400.0, 700.0)])
            _manifest(root, _UUIDS[:3])
            with self.assertRaisesRegex(ptr.UnsupportedRecovery, "ambiguous"):
                self._recover(root, root / "work", probe)

    def test_out_of_range_reference_fails_closed(self):
        pages = [_page(0, "custom PDF page 1", height=2000),
                 _page(1, "custom PDF page 3", height=2800)]

        def probe(archive, work):
            return 2, pages, []

        probe.pages = pages
        with tempfile.TemporaryDirectory(prefix="pdfrec-") as tmp:
            root = Path(tmp)
            _paper(root / "media" / "0@pdf_x.pdf",
                   [(400.0, 500.0), (400.0, 600.0)])
            _manifest(root, _UUIDS[:2])
            with self.assertRaisesRegex(ptr.UnsupportedRecovery, "references"):
                self._recover(root, root / "work", probe)

    def test_height_mismatch_fails_closed(self):
        pages = [_page(0, "custom PDF page 1", height=2600)]

        def probe(archive, work):
            return 1, pages, []

        probe.pages = pages
        with tempfile.TemporaryDirectory(prefix="pdfrec-") as tmp:
            root = Path(tmp)
            _paper(root / "media" / "0@pdf_x.pdf", [(400.0, 500.0)])
            _manifest(root, _UUIDS[:1])
            with self.assertRaisesRegex(ptr.UnsupportedRecovery, "disagrees"):
                self._recover(root, root / "work", probe)

    def test_ink_over_embedded_pdf_fails_closed(self):
        pages = [_page(0, "custom PDF page 1", height=2000, strokes=5)]

        def probe(archive, work):
            return 1, pages, []

        probe.pages = pages
        with tempfile.TemporaryDirectory(prefix="pdfrec-") as tmp:
            root = Path(tmp)
            _paper(root / "media" / "0@pdf_x.pdf", [(400.0, 500.0)])
            _manifest(root, _UUIDS[:1])
            with patch.object(ptr, "_render_all",
                              side_effect=AssertionError("render must not run")):
                with patch.object(ptr, "_probe", probe):
                    with self.assertRaisesRegex(ptr.UnsupportedRecovery, "stroke"):
                        ptr.recover(root, root / "work")
            self.assertFalse((root / "work" / "recovered.pdf").exists())

    def test_ink_over_embedded_pdf_can_be_overlaid_transparently(self):
        pages = [_page(0, "custom PDF page 1", height=2000, strokes=5)]

        def probe(archive, work):
            return 1, pages, []

        probe.pages = pages
        with tempfile.TemporaryDirectory(prefix="pdfrec-") as tmp:
            root = Path(tmp)
            _paper(root / "media" / "0@pdf_x.pdf", [(400.0, 500.0)])
            _manifest(root, _UUIDS[:1])
            render = _render(pages, extras={0: "\n1 0 0 RG 0 0 m 900 900 l S"})
            with patch.object(ptr, "_probe", probe), patch.object(ptr, "_render_all", render):
                output, count, warnings = ptr.recover(
                    root, root / "work", allow_ink_overlay=True)
            self.assertEqual(count, 1)
            self.assertTrue(output.exists())
            self.assertTrue(any("transparent ink overlay" in w for w in warnings))
            with pikepdf.open(output) as pdf:
                self.assertEqual([float(v) for v in pdf.pages[0].mediabox],
                                 [0.0, 0.0, 400.0, 500.0])
            paper = _raster(root / "media" / "0@pdf_x.pdf", 1)[1]
            overlaid = _raster(output, 1)[1]
            self.assertNotEqual(paper, overlaid)
            self.assertGreater(_mean(overlaid), 120.0)

    def test_extra_paint_on_custom_page_fails_closed(self):
        pages = [_page(0, "custom PDF page 1", height=2000)]

        def probe(archive, work):
            return 1, pages, []

        probe.pages = pages
        with tempfile.TemporaryDirectory(prefix="pdfrec-") as tmp:
            root = Path(tmp)
            _paper(root / "media" / "0@pdf_x.pdf", [(400.0, 500.0)])
            _manifest(root, _UUIDS[:1])
            render = _render(pages, extras={0: "\n1 0 0 RG 0 0 m 900 900 l S"})
            with self.assertRaisesRegex(ptr.UnsupportedRecovery, "non-background"):
                self._recover(root, root / "work", probe, render)
            self.assertFalse((root / "work" / "recovered.pdf").exists())

    def test_zero_strokes_does_not_discard_other_visible_paint(self):
        """The stroke counter excludes typed/vector paint; preserve that overlay."""
        pages = [_page(0, "custom PDF page 1", strokes=0)]

        def probe(archive, work):
            return 1, pages, []

        with tempfile.TemporaryDirectory(prefix="pdfrec-", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            paper = root / "media" / "0@pdf_x.pdf"
            _paper(paper, [(400., 500.)])
            _manifest(root, _UUIDS[:1])
            render = _render(pages, extras={0: "\n1 0 0 RG 0 0 m 900 900 l S"})
            with patch.object(ptr, "_probe", probe), patch.object(ptr, "_render_all", render):
                output, count, warnings = ptr.recover(
                    root, root / "work", allow_ink_overlay=True)
            self.assertEqual(count, 1)
            self.assertNotEqual(_raster(output, 1), _raster(paper, 1))
            self.assertTrue(any("transparent ink overlay" in warning for warning in warnings))

    def test_runs_reset_and_split_detection(self):
        pages = [_page(0, "custom PDF page 1"), _page(1, "custom PDF page 2"),
                 _page(2, "built-in 9"), _page(3, "custom PDF page 1")]
        self.assertEqual(ptr._runs(pages), [[(0, 1), (1, 2)], [(3, 1)]])
        # A numbering gap starts a new partial run instead of a rejected
        # sequence; binding that run still needs a unique geometry proof.
        self.assertEqual(ptr._runs([_page(0, "custom PDF page 1"),
                                    _page(1, "custom PDF page 3")]),
                         [[(0, 1)], [(1, 3)]])

    def test_partial_run_keeps_original_page_numbers(self):
        """Removing early pages must not renumber the remaining paper."""
        pages = [_page(0, "custom PDF page 13"),
                 _page(1, "custom PDF page 14"),
                 _page(2, "built-in 9"),
                 _page(3, "custom PDF page 4")]
        self.assertEqual(ptr._runs(pages), [[(0, 13), (1, 14)], [(3, 4)]])

    def test_partial_run_binds_uniquely_by_geometry(self):
        """A 13-based run maps to source indices 12/13, not zero/one."""
        pages = [_page(0, "custom PDF page 13", height=2400),
                 _page(1, "custom PDF page 14", height=2400)]
        sizes = {"correct.pdf": [(400., 600.)] * 14,
                 "wrong.pdf": [(400., 500.)] * 14}
        self.assertEqual(ptr._bind_pdfs(pages, sizes),
                         {0: ("correct.pdf", 12), 1: ("correct.pdf", 13)})

    def test_partial_run_does_not_bind_by_length(self):
        """A two-page fragment must never bind to a two-page attachment."""
        pages = [_page(0, "custom PDF page 13"),
                 _page(1, "custom PDF page 14")]
        sizes = {"short.pdf": [(400., 500.)] * 2,
                 "long.pdf": [(400., 500.)] * 14}
        self.assertEqual(ptr._bind_pdfs(pages, sizes),
                         {0: ("long.pdf", 12), 1: ("long.pdf", 13)})

    def test_ambiguous_partial_run_stays_failed_closed(self):
        """Matching page geometry in two attachments is not proof of identity."""
        pages = [_page(0, "custom PDF page 13")]
        sizes = {"first.pdf": [(400., 500.)] * 14,
                 "second.pdf": [(400., 500.)] * 14}
        with self.assertRaisesRegex(ptr.UnsupportedRecovery, "ambiguous"):
            ptr._bind_pdfs(pages, sizes)

    def test_gap_split_binds_partial_run_by_geometry(self):
        """Audit case: 'visible page 3 continues template run at 6'.

        The note keeps paper pages 2..4 and then resumes at page 6 (page 5
        was removed). The gap splits the run; only the page geometry may bind
        the pieces, and here it proves one attachment uniquely.
        """
        pages = [_page(0, "custom PDF page 2", height=2400),
                 _page(1, "custom PDF page 3", height=2800),
                 _page(2, "custom PDF page 4", height=3200),
                 _page(3, "custom PDF page 6", height=4000)]
        sizes = {"paper.pdf": [(400.0, 500.0 + 100.0 * n) for n in range(6)],
                 "other.pdf": [(400.0, 900.0)] * 6}
        self.assertEqual(ptr._runs(pages),
                         [[(0, 2), (1, 3), (2, 4)], [(3, 6)]])
        self.assertEqual(ptr._bind_pdfs(pages, sizes),
                         {0: ("paper.pdf", 1), 1: ("paper.pdf", 2),
                          2: ("paper.pdf", 3), 3: ("paper.pdf", 5)})

    def test_mid_document_reset_binds_partial_run_by_geometry(self):
        """Audit case: 'visible page 19 continues template run at 2'.

        Paper pages 4..22 are visible first; a later section resumes at page
        2. The reset splits the run and the geometry must prove both pieces
        belong to the same attachment — never by run length.
        """
        pages = [_page(index, f"custom PDF page {number}",
                       height=1600 + 400 * number)
                 for index, number in enumerate(range(4, 23))]
        pages.append(_page(19, "custom PDF page 2", height=1600 + 400 * 2))
        sizes = {"paper.pdf": [(400.0, 500.0 + 100.0 * n) for n in range(22)],
                 "other.pdf": [(400.0, 999.0)] * 25}
        self.assertEqual(ptr._runs(pages),
                         [[(index, index + 4) for index in range(19)], [(19, 2)]])
        expected = {index: ("paper.pdf", number - 1)
                    for index, number in enumerate(range(4, 23))}
        expected[19] = ("paper.pdf", 1)
        self.assertEqual(ptr._bind_pdfs(pages, sizes), expected)

    def test_gap_at_page_50_splits_the_long_run_and_binds_by_geometry(self):
        """Audit case: 'visible page 50 continues template run at 23'.

        A complete 1..50 stretch is followed by a page that references paper
        page 23 again, so the run splits at the gap and both pieces bind only
        through the unique geometry assignment.
        """
        pages = [_page(index, f"custom PDF page {index + 1}",
                       height=1600 + 400 * (index + 1))
                 for index in range(50)]
        pages.append(_page(50, "custom PDF page 23", height=1600 + 400 * 23))
        sizes = {"paper.pdf": [(400.0, 500.0 + 100.0 * n) for n in range(50)],
                 "other.pdf": [(400.0, 999.0)] * 50}
        self.assertEqual(ptr._runs(pages),
                         [[(index, index + 1) for index in range(50)], [(50, 23)]])
        expected = {index: ("paper.pdf", index) for index in range(50)}
        expected[50] = ("paper.pdf", 22)
        self.assertEqual(ptr._bind_pdfs(pages, sizes), expected)

    def test_gap_before_page_22_splits_and_binds_by_geometry(self):
        """Audit case: 'visible page 2 continues template run at 22'.

        Paper pages 19..20 are followed by page 22 (page 21 was removed);
        the two-page fragment must not be guessed from its length.
        """
        pages = [_page(0, "custom PDF page 19", height=1600 + 400 * 19),
                 _page(1, "custom PDF page 20", height=1600 + 400 * 20),
                 _page(2, "custom PDF page 22", height=1600 + 400 * 22)]
        sizes = {"paper.pdf": [(400.0, 500.0 + 100.0 * n) for n in range(22)],
                 "other.pdf": [(400.0, 999.0)] * 25}
        self.assertEqual(ptr._runs(pages), [[(0, 19), (1, 20)], [(2, 22)]])
        self.assertEqual(ptr._bind_pdfs(pages, sizes),
                         {0: ("paper.pdf", 18), 1: ("paper.pdf", 19),
                          2: ("paper.pdf", 21)})

    def test_reused_document_with_overlapping_geometry_stays_ambiguous(self):
        """One attachment may back several runs; never consume it to force a pick.

        A run that can only be the attachment used by an earlier run, or an
        attachment with overlapping geometry, is unproven and must fail closed.
        The old solver excluded already-used documents and silently mis-bound
        the second run to an unrelated attachment.
        """
        pages = [_page(0, "custom PDF page 1"),
                 _page(1, "built-in 9"),
                 _page(2, "custom PDF page 5")]
        sizes = {
            "a.pdf": [(400., 500.)] * 6,
            "b.pdf": [(400., 600.), (400., 600.),
                      (400., 500.), (400., 500.), (400., 500.), (400., 500.)],
        }
        self.assertEqual(ptr._runs(pages), [[(0, 1)], [(2, 5)]])
        with self.assertRaisesRegex(ptr.UnsupportedRecovery, "ambiguous"):
            ptr._bind_pdfs(pages, sizes)

    def test_single_document_may_back_multiple_runs(self):
        """A unique geometry match still binds when one source serves two runs."""
        pages = [_page(0, "custom PDF page 1"),
                 _page(1, "built-in 9"),
                 _page(2, "custom PDF page 5")]
        sizes = {"a.pdf": [(400., 500.)] * 6,
                 "other.pdf": [(400., 600.)] * 6}
        self.assertEqual(ptr._bind_pdfs(pages, sizes),
                         {0: ("a.pdf", 0), 2: ("a.pdf", 4)})

    def test_zero_page_reference_is_rejected(self):
        """Reject invalid numbering before it can become a negative index."""
        with self.assertRaisesRegex(ptr.UnsupportedRecovery, "positive"):
            ptr._runs([_page(0, "custom PDF page 0")])

    def test_pinned_quarter_turn_height_is_accepted_only_by_geometry(self):
        """Audit case: 'visible page 0 height 899 disagrees ... (scaled 1801.5)'.

        The pinned page presents a portrait source sheet turned a quarter
        turn, so its reported height matches the transposed MediaBox
        orientation. A height matching neither orientation still fails.
        """
        pages = [_page(0, "custom PDF page 1", width=1272, height=899)]
        sizes = {"paper.pdf": [(600.0, 850.0)]}
        self.assertEqual(ptr._bind_pdfs(pages, sizes), {0: ("paper.pdf", 0)})
        with self.assertRaisesRegex(ptr.UnsupportedRecovery, "disagrees"):
            ptr._bind_pdfs([_page(0, "custom PDF page 1", width=1272, height=1400)],
                           sizes)

    def test_null_navigation_destination_is_dropped_not_crashed(self):
        """Audit case: \"'NoneType' object has no attribute 'objgen'\".

        pikepdf exposes a PDF null destination target as Python None; the
        destination names no page and must be dropped like an omitted page.
        """
        pages = [_page(0, "custom PDF page 1"), _page(1, "custom PDF page 2")]

        def probe(archive, work):
            return 2, pages, []

        probe.pages = pages
        with tempfile.TemporaryDirectory(prefix="pdfrec-", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            paper = root / "media" / "0@pdf_nulldest.pdf"
            _paper(paper, [(400., 500.)] * 2)
            edited = root / "paper.pdf"
            with pikepdf.open(paper) as pdf:
                null_target = pikepdf.Array([pikepdf.Object.parse(b"null"),
                                             pikepdf.Name.Fit])
                pdf.Root.Names = pikepdf.Dictionary(Dests=pdf.make_indirect(
                    pikepdf.Dictionary(Names=pikepdf.Array(
                        [pikepdf.String("start"), null_target]))))
                pdf.save(edited)
            shutil.copyfile(edited, paper)
            _manifest(root, _UUIDS[:2])
            output, count, _ = self._recover(root, root / "work", probe)
            self.assertEqual(count, 2)
            with ptr._fitz().open(output) as pdf:
                self.assertNotIn("start", pdf.resolve_names())

    def test_reordered_paper_keeps_bookmarks_named_destinations_and_links(self):
        """Catalog navigation must target the correct copied page, not its old index."""
        pages = [_page(0, "custom PDF page 2"), _page(1, "custom PDF page 1")]

        def probe(archive, work):
            return 2, pages, []

        probe.pages = pages
        with tempfile.TemporaryDirectory(prefix="pdfrec-", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            paper = root / "media" / "0@pdf_navigation.pdf"
            _paper(paper, [(400., 500.)] * 2)
            edited = root / "paper.pdf"
            with pikepdf.open(paper) as pdf:
                first = pikepdf.OutlineItem("First", 0)
                first.children = [pikepdf.OutlineItem("Second", 1)]
                with pdf.open_outline() as outline:
                    outline.root = [first]
                destination = pikepdf.Array([pdf.pages[0].obj, pikepdf.Name.Fit])
                pdf.Root.Names = pikepdf.Dictionary(Dests=pdf.make_indirect(
                    pikepdf.Dictionary(Names=pikepdf.Array([pikepdf.String("start"), destination]))))
                pdf.pages[1].obj.Annots = pikepdf.Array([
                    pdf.make_indirect(pikepdf.Dictionary(
                        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link,
                        Rect=pikepdf.Array([10, 10, 100, 30]),
                        Dest=pikepdf.String("start"))),
                    pdf.make_indirect(pikepdf.Dictionary(
                        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link,
                        Rect=pikepdf.Array([10, 50, 100, 70]),
                        A=pikepdf.Dictionary(S=pikepdf.Name.URI,
                                             URI=pikepdf.String("https://example.org/"))))])
                pdf.save(edited)
            shutil.copyfile(edited, paper)
            _manifest(root, _UUIDS[:2])
            output, count, _ = self._recover(root, root / "work", probe)
            self.assertEqual(count, 2)
            with ptr._fitz().open(output) as pdf:
                self.assertEqual(pdf.get_toc(), [[1, "First", 2], [2, "Second", 1]])
                links = pdf[0].get_links()
                self.assertTrue(any(link.get("page") == 1 for link in links), links)
                self.assertTrue(any(link.get("uri") == "https://example.org/" for link in links), links)
                self.assertEqual(pdf.resolve_names()["start"]["page"], 1)

    def test_native_only_note_is_out_of_scope(self):
        pages = [_page(0, "built-in 9", height=2262)]

        def probe(archive, work):
            return 1, pages, []

        probe.pages = pages
        with tempfile.TemporaryDirectory(prefix="pdfrec-") as tmp:
            root = Path(tmp)
            _paper(root / "media" / "0@pdf_x.pdf", [(400.0, 500.0)])
            _manifest(root, _UUIDS[:1])
            with self.assertRaisesRegex(ptr.UnsupportedRecovery, "no custom-PDF"):
                self._recover(root, root / "work", probe)


@unittest.skipUnless(pikepdf, "pikepdf is required")
@unittest.skipUnless(REAL, "real-sample recovery disabled")
@unittest.skipUnless(SAMPLES.exists(), "stage2 source snapshots are absent")
class RealSampleRecoveryTests(unittest.TestCase):
    """Full pipeline against the three supplied legacy snapshots."""

    @staticmethod
    def _sample(prefix: str) -> Path:
        """Locate a snapshot note tree (media/ + pageIdInfo.dat) by uuid prefix."""
        for manifest in sorted(SAMPLES.rglob("pageIdInfo.dat")):
            note = manifest.parent
            if prefix in note.name or prefix in note.parent.name:
                return note
        raise AssertionError(f"sample {prefix} not found under {SAMPLES}")

    def _run(self, prefix: str):
        source = self._sample(prefix)
        work = Path(tempfile.mkdtemp(prefix=f"pdfrec-{prefix}-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        return ptr.recover(source, work / "out"), source

    def test_pure_paper_note_recovers_fully(self):
        (output, count, warnings), _source = self._run("019bab7a4337")
        self.assertEqual(count, 56)
        self.assertTrue(any("spliced 54 embedded PDF page(s)" in w for w in warnings))
        ptr._ie().verify(output, 56)

    def test_multi_document_note_recovers_fully(self):
        (output, count, warnings), _source = self._run("019e1a62b0f9")
        self.assertEqual(count, 33)
        self.assertTrue(any("spliced 32 embedded PDF page(s)" in w for w in warnings))
        ptr._ie().verify(output, 33)

    def test_ink_over_paper_note_fails_closed(self):
        with self.assertRaisesRegex(ptr.UnsupportedRecovery, "stroke"):
            self._run("0199fd15dbda")


if __name__ == "__main__":
    unittest.main()
