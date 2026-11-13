"""Unit tests for the light-canvas PDF normalizer.

The fixtures are synthetic one-page PDFs so the tests never depend on the
exported corpus.  They check the colour rule, the background fill rewrite,
the transparent ink layer, and the guarantee that light pages are untouched.
"""
import tempfile
import unittest
from pathlib import Path

import pdf_color_normalize as pcn

try:
    import pikepdf
except ImportError:  # pragma: no cover - environment without pikepdf
    pikepdf = None


DARK_PAGE = (
    b"q 0.75 0 0 -0.75 0 2400 cm 0.003921569 g 0 0 m 1600 0 l "
    b"1600 3200 l 0 3200 l h f Q\n"
    b"q 0.75 0 0 -0.75 0 2400 cm 1 0.79607847 0.1882353 RG 1 J 0.33 w 4 M "
    b"100 100 m 200 200 l S Q\n"
)
LIGHT_PAGE = (
    b"q 1 0 0 1 0 0 cm 1 g 0 0 m 600 0 l 600 800 l 0 800 l h f Q\n"
    b"q 1 0 0 1 0 0 cm 0 0 1 RG 2 w 100 100 m 200 200 l S Q\n"
)


class ReverseColorTests(unittest.TestCase):
    """The rule mirrors sdocx's ``reverse_color``."""

    def test_neutral_band_is_untouched(self):
        self.assertEqual(pcn.reverse_rgb(255, 203, 48), (255, 203, 48))
        self.assertEqual(pcn.reverse_rgb(51, 150, 255), (51, 150, 255))
        self.assertEqual(pcn.reverse_rgb(128, 128, 128), (128, 128, 128))
        self.assertEqual(pcn.reverse_rgb(102, 102, 102), (102, 102, 102))

    def test_bright_colour_is_darkened_outside_the_band(self):
        self.assertEqual(pcn.reverse_rgb(71, 237, 135), (18, 184, 82))

    def test_extremes_invert(self):
        self.assertEqual(pcn.reverse_rgb(255, 255, 255), (0, 0, 0))
        self.assertEqual(pcn.reverse_rgb(0, 0, 0), (255, 255, 255))


class AdaptColorTests(unittest.TestCase):
    """Ink is re-tuned for the white canvas by contrast, not by hue flipping."""

    def test_bright_ink_is_darkened_for_white_paper(self):
        self.assertEqual(pcn.adapt_rgb(255, 203, 48), (207, 155, 0))
        self.assertEqual(pcn.adapt_rgb(51, 150, 255), (0, 99, 204))

    def test_dark_ink_stays_dark(self):
        self.assertEqual(pcn.adapt_rgb(0, 0, 0), (0, 0, 0))

    def test_white_ink_becomes_black(self):
        self.assertEqual(pcn.adapt_rgb(255, 255, 255), (0, 0, 0))

    def test_gray_ink_prefers_the_more_readable_variant(self):
        self.assertEqual(pcn.adapt_gray(255.0), 0.0)
        self.assertEqual(pcn.adapt_gray(0.0), 0.0)
        self.assertAlmostEqual(pcn.adapt_gray(128.0), 128.0, delta=1.0)


class ContentStreamTests(unittest.TestCase):
    """The background fill and the ink colours are rewritten separately."""

    def test_dark_background_becomes_white(self):
        data, facts = pcn.normalize_content_stream(DARK_PAGE)
        self.assertEqual(facts["background_fills"], 1)
        self.assertIn(b"1 g 0 0 m 1600 0 l", data)
        self.assertNotIn(b"0.003921569 g", data)

    def test_ink_colour_is_retuned_for_the_white_canvas(self):
        data, facts = pcn.normalize_content_stream(DARK_PAGE)
        self.assertGreater(facts["color_operators"], 0)
        self.assertNotIn(b"1 0.79607847 0.1882353 RG", data)
        self.assertIn(b"0.81176471 0.60784314 0.00000000 RG", data)

    def test_strip_background_keeps_the_stack_balanced(self):
        data, removed = pcn.strip_background_stream(DARK_PAGE)
        self.assertEqual(removed, 1)
        self.assertNotIn(b"0.003921569 g", data)
        self.assertEqual(data.count(b" q "), data.count(b" Q "))

    def test_light_pages_are_not_modified(self):
        data, facts = pcn.normalize_content_stream(LIGHT_PAGE, reverse_colors=False)
        self.assertEqual(data, LIGHT_PAGE)
        self.assertEqual(facts["background_fills"], 0)


class GeometryScannerTests(unittest.TestCase):
    """The transform-aware scanner handles both background styles."""

    CLI_PAGE = (
        b"q 0.75 0 0 -0.75 0 2400 cm 0.003921569 g 0 0 m 1600 0 l 1600 3200 l 0 3200 l\n"
        b"h\nf\nQ\n"
        b"q 0.75 0 0 -0.75 0 2400 cm 1 0.79607847 0.1882353 RG 1 J 0.33 w 4 M "
        b"100 100 m 200 200 l S Q\n"
    )
    RECT_PAGE = (
        b"\nq\n0 0 720 405 re\nh\n0 0 0 rg f\nQ\n"
        b"q\nBT\n/TT0 1 Tf\n(MIT OpenCourseWare ) Tj\nET\nQ\n"
    )

    def test_cli_background_keeps_its_colour_operator(self):
        data, facts = pcn.normalize_content_stream(self.CLI_PAGE, page_size=(1200.0, 2400.0))
        self.assertEqual(facts["background_fills"], 1)
        self.assertIn(b"1 g 0 0 m 1600 0 l", data)
        self.assertNotIn(b"0.003921569 g", data)
        self.assertNotIn(b".003921569 1", data)

    def test_rectangle_background_is_whitened(self):
        data, facts = pcn.normalize_content_stream(self.RECT_PAGE, page_size=(720.0, 405.0))
        self.assertEqual(facts["background_fills"], 1)
        self.assertIn(b"1 1 1 rg f", data)
        self.assertNotIn(b"0 0 0 rg f", data)

    def test_light_background_is_preserved(self):
        page = b"q 0 0 600 800 re h 1 1 1 rg f Q\n"
        data, facts = pcn.normalize_content_stream(page, page_size=(600.0, 800.0))
        self.assertEqual(facts["background_fills"], 0)
        self.assertIn(b"1 1 1 rg f", data)

    def test_strip_removes_the_operator_as_well(self):
        stripped, removed = pcn.strip_background_stream(self.CLI_PAGE, (1200.0, 2400.0))
        self.assertEqual(removed, 1)
        self.assertNotIn(b"0.003921569", stripped)
        self.assertNotIn(b"1600 0 l", stripped)
        self.assertIn(b"0.79607847", stripped)


@unittest.skipIf(pikepdf is None, "pikepdf is required")
class NormalizePdfTests(unittest.TestCase):
    """End-to-end repair of a synthetic page with pypdfium2 verification."""

    def _write(self, path: Path, content: bytes) -> None:
        with pikepdf.Pdf.new() as pdf:
            page = pdf.add_blank_page(page_size=(600, 800))
            page.obj["/Contents"] = pdf.make_stream(content)
            pdf.save(path)

    def test_dark_page_is_whitened_and_keeps_its_ink(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "dark.pdf"
            output = Path(temporary) / "light.pdf"
            self._write(source, DARK_PAGE)
            report = pcn.normalize_pdf(source, output)
            self.assertEqual(report.repaired_pages, [0])
            stats = pcn.render_page_stats(output)
            self.assertEqual(len(stats), 1)
            self.assertGreater(stats[0]["mean"], 120.0)

    def test_light_page_is_left_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "light.pdf"
            output = Path(temporary) / "light-out.pdf"
            self._write(source, LIGHT_PAGE)
            report = pcn.normalize_pdf(source, output)
            self.assertEqual(report.repaired_pages, [])
            self.assertEqual(source.read_bytes()[:5], b"%PDF-")
            stats = pcn.render_page_stats(output)
            self.assertGreater(stats[0]["mean"], 200.0)


if __name__ == "__main__":
    unittest.main()
