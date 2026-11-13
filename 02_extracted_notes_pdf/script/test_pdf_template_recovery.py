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

    def test_runs_reset_and_split_detection(self):
        pages = [_page(0, "custom PDF page 1"), _page(1, "custom PDF page 2"),
                 _page(2, "built-in 9"), _page(3, "custom PDF page 1")]
        self.assertEqual(ptr._runs(pages), [[(0, 1), (1, 2)], [(3, 1)]])
        with self.assertRaisesRegex(ptr.UnsupportedRecovery, "continues template run"):
            ptr._runs([_page(0, "custom PDF page 1"), _page(1, "custom PDF page 3")])

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
