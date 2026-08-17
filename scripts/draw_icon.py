"""Draw softedibo.png from vector code (dev tool, run by hand).

The application artwork is a wordmark: an "SE" monogram over the project name,
on a rounded tile. It is generated rather than hand-painted so it stays crisp
at every icon size - the text is converted to outlines in a 1024 x 1024 design
grid, scaled to fit its box, and rendered with antialiasing at the requested
output size.

    python scripts/draw_icon.py                    # write softedibo.png (256 px)
    python scripts/draw_icon.py --variant stacked  # try another layout
    python scripts/draw_icon.py --out /tmp/x.png --size 512

Run scripts/make_icon.py afterwards to rebuild the Windows .ico.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QRadialGradient,
    QTransform,
)

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "softedibo.png"

# Everything is drawn in this square grid and scaled to the output size, so the
# same artwork works for a 16 px tray icon and a 256 px launcher tile.
GRID = 1024.0

# The app's existing blue identity, deepened so white text keeps its contrast.
TILE_TOP = QColor("#2E9BD6")
TILE_BOTTOM = QColor("#0E4272")
TEXT = QColor("#FFFFFF")
ACCENT = QColor("#7FE3D4")       # "Soft"
HIGHLIGHT = QColor("#FFC24D")    # "IBO" - warm, so it separates from the teal

# First family present on the machine wins; the last one ships with Qt itself.
FONT_FAMILIES = ("Ubuntu", "Inter", "Noto Sans", "DejaVu Sans")

# Text is drawn as outlines at this size and then scaled, so the value only
# sets the resolution of the curves, never the size on the tile.
OUTLINE_PIXELS = 256


class Run:
    """One stretch of text in one colour, e.g. the "IBO" of "SoftEdIBO"."""

    def __init__(self, text: str, colour: QColor) -> None:
        self.text = text
        self.colour = colour


class Line:
    """A row of coloured runs, laid out on one baseline and fitted as a block.

    The runs are measured and scaled together, so recolouring part of the name
    never moves the letters: the line looks exactly like single-colour text.
    """

    def __init__(self, *runs: Run, weight: QFont.Weight = QFont.Weight.Bold,
                 tracking: float = 100.0) -> None:
        self._runs = runs
        self._weight = weight
        self._tracking = tracking

    def _font(self) -> QFont:
        font = QFont()
        font.setFamilies(list(FONT_FAMILIES))
        font.setPixelSize(OUTLINE_PIXELS)
        font.setWeight(self._weight)
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, self._tracking)
        return font

    def fit(self, box: QRectF) -> list[tuple[QPainterPath, QColor]]:
        """Return the runs as outlines, scaled to fill ``box`` and centred in it."""
        font = self._font()
        metrics = QFontMetricsF(font)

        pieces: list[tuple[QPainterPath, QColor]] = []
        cursor = 0.0
        for run in self._runs:
            path = QPainterPath()
            path.addText(cursor, 0.0, font, run.text)
            pieces.append((path, run.colour))
            cursor += metrics.horizontalAdvance(run.text)

        bounds = QRectF()
        for path, _ in pieces:
            bounds = bounds.united(path.boundingRect())
        if bounds.isEmpty():
            return pieces

        scale = min(box.width() / bounds.width(), box.height() / bounds.height())
        transform = QTransform().scale(scale, scale)
        scaled = [(transform.map(path), colour) for path, colour in pieces]

        shift = box.center() - QPointF(bounds.center().x() * scale,
                                       bounds.center().y() * scale)
        return [(path.translated(shift), colour) for path, colour in scaled]


class Artwork:
    """Base class for one icon layout.

    Subclasses paint into a painter whose coordinate system is the 1024 x 1024
    design grid, with the rounded tile already filled and clipped.
    """

    name = "artwork"

    def draw_body(self, painter: QPainter) -> None:
        raise NotImplementedError

    # -- shared pieces ---------------------------------------------------

    def draw_tile(self, painter: QPainter) -> QPainterPath:
        """Fill the rounded background tile and return its path (for clipping)."""
        tile = QPainterPath()
        tile.addRoundedRect(QRectF(0, 0, GRID, GRID), 0.225 * GRID, 0.225 * GRID)

        gradient = QLinearGradient(0.15 * GRID, 0.0, 0.85 * GRID, GRID)
        gradient.setColorAt(0.0, TILE_TOP)
        gradient.setColorAt(1.0, TILE_BOTTOM)
        painter.fillPath(tile, QBrush(gradient))

        # A wide light pool behind the text lifts it off the flat gradient.
        glow = QRadialGradient(QPointF(0.5 * GRID, 0.44 * GRID), 0.55 * GRID)
        glow.setColorAt(0.0, QColor(255, 255, 255, 38))
        glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillPath(tile, QBrush(glow))
        return tile

    def draw_line(self, painter: QPainter, line: Line, box: QRectF) -> None:
        """Draw ``line`` inside ``box``, with a soft shadow so it reads on any tile."""
        pieces = line.fit(box)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(4, 26, 48, 70))
        for path, _ in pieces:
            painter.drawPath(path.translated(0.0, 0.012 * GRID))
        for path, colour in pieces:
            painter.setBrush(colour)
            painter.drawPath(path)


class StackedArtwork(Artwork):
    """"Soft" over "EdIBO": two short lines set as large as the tile allows.

    Splitting the name is what makes it legible small - one line of nine
    characters is a smear by the time the icon reaches the taskbar. The three
    colours also split the name into its parts: Soft / Ed / IBO.
    """

    name = "stacked"

    def draw_body(self, painter: QPainter) -> None:
        self.draw_line(
            painter,
            Line(Run("Soft", ACCENT), tracking=98.0),
            QRectF(0.15 * GRID, 0.235 * GRID, 0.70 * GRID, 0.255 * GRID))
        self.draw_line(
            painter,
            Line(Run("Ed", TEXT), Run("IBO", HIGHLIGHT), tracking=98.0),
            QRectF(0.13 * GRID, 0.545 * GRID, 0.74 * GRID, 0.255 * GRID))


class LineArtwork(Artwork):
    """The full name on one line - closest to the old artwork, but set properly."""

    name = "line"

    def draw_body(self, painter: QPainter) -> None:
        self.draw_line(
            painter,
            Line(Run("Soft", ACCENT), Run("Ed", TEXT), Run("IBO", HIGHLIGHT),
                 tracking=97.0),
            QRectF(0.10 * GRID, 0.40 * GRID, 0.80 * GRID, 0.20 * GRID))


class TrioArtwork(Artwork):
    """One line per part - the name read as the three acronyms it is made of.

    Each line is fitted to a box of the same height, so the parts share a cap
    height and only their widths differ.
    """

    name = "trio"

    PARTS = (("Soft", ACCENT), ("Ed", TEXT), ("IBO", HIGHLIGHT))

    def draw_body(self, painter: QPainter) -> None:
        height = 0.175 * GRID
        for index, (text, colour) in enumerate(self.PARTS):
            top = (0.185 + index * 0.215) * GRID
            self.draw_line(
                painter,
                Line(Run(text, colour), tracking=98.0),
                QRectF(0.14 * GRID, top, 0.72 * GRID, height))


class MonogramArtwork(Artwork):
    """A big "SE" for icon sizes, with the full name underneath for large ones."""

    name = "mono"

    def draw_body(self, painter: QPainter) -> None:
        self.draw_line(
            painter,
            Line(Run("SE", TEXT), weight=QFont.Weight.Black, tracking=94.0),
            QRectF(0.18 * GRID, 0.22 * GRID, 0.64 * GRID, 0.38 * GRID))
        self.draw_line(
            painter,
            Line(Run("Soft", ACCENT), Run("Ed", TEXT), Run("IBO", HIGHLIGHT),
                 weight=QFont.Weight.DemiBold, tracking=112.0),
            QRectF(0.16 * GRID, 0.685 * GRID, 0.68 * GRID, 0.095 * GRID))


VARIANTS = {
    art.name: art
    for art in (StackedArtwork(), TrioArtwork(), LineArtwork(), MonogramArtwork())
}


class IconRenderer:
    """Render an :class:`Artwork` to a square image of any size."""

    def __init__(self, artwork: Artwork) -> None:
        self._artwork = artwork

    def render(self, size: int) -> QImage:
        # Supersample: draw large, then let the smooth scaler resolve the thin
        # letter strokes instead of aliasing them at small icon sizes.
        supersample = max(size, 512) * 2
        image = QImage(supersample, supersample, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)

        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(supersample / GRID, supersample / GRID)
        tile = self._artwork.draw_tile(painter)
        painter.setClipPath(tile)
        self._artwork.draw_body(painter)
        painter.end()

        if supersample == size:
            return image
        return image.scaled(
            size, size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=sorted(VARIANTS), default="mono")
    parser.add_argument("--size", type=int, default=256)
    parser.add_argument("--out", type=Path, default=TARGET)
    args = parser.parse_args(argv)

    # Text needs the font database, which only exists once a QGuiApplication
    # does; no window is ever shown.
    app = QGuiApplication.instance() or QGuiApplication(["draw_icon"])

    image = IconRenderer(VARIANTS[args.variant]).render(args.size)
    # No explicit format: Qt picks the writer from the .png suffix, and the
    # PySide6 stub disagrees with the runtime about the argument's type.
    if not image.save(str(args.out)):
        raise SystemExit(f"cannot write {args.out}")
    print(f"wrote {args.out} ({args.size}px, variant {args.variant})")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
