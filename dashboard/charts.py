"""Chart builders.

Pure functions: rows in, a Plotly figure out. No Streamlit, no HTTP, no
computation beyond turning the API's fractional rates into percentages for an
axis -- every number plotted here was calculated by the reporting service, so
there is one definition of "attendance rate" and the chart cannot drift from
the table printed beside it.

Being pure is also what makes them testable: a test can assert on the trace
data without a browser.
"""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go

from dashboard.theme import Palette, status_colours

# Mark specs, applied uniformly.
LINE_WIDTH = 2
MARKER_SIZE = 8
CORNER_RADIUS = 4
# A gap between adjacent fills, drawn in the surface colour so stacked
# segments read as separate marks rather than one smeared band.
SEGMENT_GAP = 2

# Room above the plot for the title, and for a legend when there is one.
TOP_MARGIN = 44
TOP_MARGIN_WITH_LEGEND = 78


def _layout(
    fig: go.Figure,
    pal: Palette,
    *,
    title: str,
    height: int = 320,
    legend: bool = False,
) -> go.Figure:
    """Recessive frame: the data is the only thing with weight.

    ``legend`` widens the top margin to make room for one. Plotly places both
    the title and a legend at ``y >= 1`` in the same band, so a legend drawn
    above the plot lands on top of the title unless the margin is opened for
    it -- which is invisible in the trace data and only shows up when the
    chart is actually rendered and looked at.
    """
    top = TOP_MARGIN_WITH_LEGEND if legend else TOP_MARGIN
    fig.update_layout(
        title={
            "text": title,
            "font": {"size": 15, "color": pal.text_primary},
            "y": 1,
            "yanchor": "top",
            "pad": {"t": 12},
        },
        paper_bgcolor=pal.surface,
        plot_bgcolor=pal.surface,
        font={"color": pal.text_secondary, "size": 12},
        margin={"l": 8, "r": 8, "t": top, "b": 8},
        height=height,
        hoverlabel={"bgcolor": pal.surface, "font": {"color": pal.text_primary}},
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "x": 0,
            "font": {"color": pal.text_secondary},
            "title": {"text": ""},
        },
    )
    fig.update_xaxes(
        showgrid=False,
        linecolor=pal.grid,
        tickfont={"color": pal.text_muted},
        zeroline=False,
    )
    fig.update_yaxes(
        gridcolor=pal.grid,
        linecolor=pal.grid,
        tickfont={"color": pal.text_muted},
        zeroline=False,
    )
    return fig


def _empty(pal: Palette, title: str, message: str) -> go.Figure:
    """A chart with nothing to draw says so, rather than drawing empty axes."""
    fig = go.Figure()
    fig.add_annotation(
        text=message,
        showarrow=False,
        font={"color": pal.text_muted, "size": 13},
        xref="paper",
        yref="paper",
        x=0.5,
        y=0.5,
    )
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return _layout(fig, pal, title=title, height=220)


def attendance_trend(points: list[dict[str, Any]], pal: Palette) -> go.Figure:
    """Attendance rate over time -- one measure, one line, no legend.

    Absence rate is deliberately not plotted alongside: it is 1 - attendance
    over the same base, so a second line would carry no information the first
    does not, while doubling what the reader has to track.
    """
    if not points:
        return _empty(pal, "Attendance rate over time", "No periods in this range.")

    labels = [p["period_start"] for p in points]
    values = [round(p["attendance_rate"] * 100, 1) for p in points]

    fig = go.Figure(
        go.Scatter(
            x=labels,
            y=values,
            mode="lines+markers",
            name="Attendance rate",
            line={"color": pal.series, "width": LINE_WIDTH},
            marker={"color": pal.series, "size": MARKER_SIZE},
            customdata=[(p["attended"], p["expected"]) for p in points],
            hovertemplate=(
                "<b>%{x}</b><br>%{y}% attended"
                "<br>%{customdata[0]} of %{customdata[1]} expected<extra></extra>"
            ),
        )
    )
    # A crosshair, so a reader can line a point up against the axis.
    fig.update_layout(hovermode="x unified", showlegend=False)
    fig.update_yaxes(range=[0, 100], ticksuffix="%")
    return _layout(fig, pal, title="Attendance rate over time")


def status_composition(summaries: list[dict[str, Any]], pal: Palette) -> go.Figure:
    """How each day was made up -- counts of people, stacked.

    Counts rather than percentages, and one axis: a day with three expected
    people and a day with three hundred should not look alike.
    """
    if not summaries:
        return _empty(pal, "Daily composition", "No days in this range.")

    days = [s["work_date"] for s in summaries]
    # Present excludes the late, so the segments sum to the expected headcount
    # instead of double-counting someone who arrived late.
    series = {
        "Present": [s["present"] - s["late"] for s in summaries],
        "On leave": [s["on_leave"] for s in summaries],
        "Late": [s["late"] for s in summaries],
        "Absent": [s["absent"] for s in summaries],
    }
    colours = status_colours(pal)

    fig = go.Figure()
    for label, values in series.items():
        fig.add_bar(
            x=days,
            y=values,
            name=label,
            marker={
                "color": colours[label],
                "cornerradius": CORNER_RADIUS,
                "line": {"color": pal.surface, "width": SEGMENT_GAP},
            },
            hovertemplate=f"<b>%{{x}}</b><br>{label}: %{{y}}<extra></extra>",
        )
    fig.update_layout(barmode="stack", bargap=0.35)
    return _layout(
        fig, pal, title="Daily composition (people)", height=360, legend=True
    )


def absenteeism_by_group(rows: list[dict[str, Any]], pal: Palette) -> go.Figure:
    """Absence rate by department or employee -- one measure, ranked.

    Horizontal, because group labels are words and words fit along a y-axis.
    One hue throughout: the bar's length already carries the magnitude, and
    colouring by rank would repaint every bar whenever a filter changed.
    """
    if not rows:
        return _empty(pal, "Absence rate by group", "No groups in this range.")

    # Largest at the top: Plotly draws the first category at the bottom.
    ordered = sorted(rows, key=lambda r: r["absence_rate"])
    labels = [r["group_label"] for r in ordered]
    values = [round(r["absence_rate"] * 100, 1) for r in ordered]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker={
                "color": pal.series,
                "cornerradius": CORNER_RADIUS,
                "line": {"color": pal.surface, "width": SEGMENT_GAP},
            },
            # Every bar is labelled, which is the relief for any reader who
            # cannot separate the bar from its background.
            text=[f"{v}%" for v in values],
            textposition="outside",
            textfont={"color": pal.text_secondary},
            customdata=[(r["absent_days"], r["expected_days"]) for r in ordered],
            hovertemplate=(
                "<b>%{y}</b><br>%{x}% absent"
                "<br>%{customdata[0]} of %{customdata[1]} expected days<extra></extra>"
            ),
        )
    )
    fig.update_layout(showlegend=False, bargap=0.35)
    fig.update_xaxes(ticksuffix="%", range=[0, max(max(values) * 1.18, 5)])
    return _layout(
        fig, pal, title="Absence rate by group", height=max(240, 38 * len(rows) + 90)
    )
