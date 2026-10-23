"""Chart colours and the rules about which one to use.

Three jobs, three sources of colour, and they are not interchangeable:

* **Magnitude** (one series -- an attendance rate over time, absence rate by
  department) uses the single sequential hue. One series needs no legend; the
  chart title names it.
* **State** (present / late / absent / on leave) uses the *status* palette.
  These are reserved colours: ``late`` is amber because late is a warning, not
  because it happened to be the second series. They never stand in for a
  series, and they never carry meaning alone -- every chart using them ships a
  legend and a table view beside it.
* **Ink** (axes, labels, values) uses text tokens, never a series colour, so a
  number never looks like it belongs to a mark.

Both modes are selected rather than flipped: the dark steps were chosen
against the dark surface, not derived by inverting the light ones.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    """Every colour a chart may use, resolved for one mode."""

    surface: str
    text_primary: str
    text_secondary: str
    text_muted: str
    grid: str

    # Magnitude: one hue. Validated >= 3:1 against its own surface in both modes.
    series: str

    # State. Fixed in both modes by design -- a status colour means the same
    # thing wherever it appears, so it is never re-stepped.
    good: str = "#0ca30c"
    warning: str = "#fab219"
    serious: str = "#ec835a"
    critical: str = "#d03b3b"

    # "On leave" is an authorised absence: deliberately low-chroma so it
    # recedes behind the states that need attention.
    neutral: str = "#8a8a82"


LIGHT = Palette(
    surface="#fcfcfb",
    text_primary="#0b0b0b",
    text_secondary="#52514e",
    text_muted="#78776f",
    grid="#e8e7e2",
    series="#2a78d6",
)

DARK = Palette(
    surface="#1a1a19",
    text_primary="#f0efec",
    text_secondary="#c3c2b7",
    text_muted="#8f8e85",
    grid="#2f2f2c",
    series="#3987e5",
)


def palette(dark: bool = False) -> Palette:
    return DARK if dark else LIGHT


# The order statuses are stacked in, worst at the top of the legend so the
# thing a manager is looking for is the thing they read first. Each pairs a
# label with its colour: identity is never colour alone.
def status_colours(pal: Palette) -> dict[str, str]:
    return {
        "Absent": pal.critical,
        "Late": pal.warning,
        "On leave": pal.neutral,
        "Present": pal.good,
    }


# Shown beside a status wherever one appears outside a chart, so the meaning
# survives greyscale, printing and colour-blindness.
STATUS_ICONS = {
    "Absent": ":material/cancel:",
    "Late": ":material/schedule:",
    "On leave": ":material/event_available:",
    "Present": ":material/check_circle:",
}
