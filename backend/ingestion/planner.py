
"""QuantDesk historical fetch planner (no HTTP, SQLite, GUI, or GDP processing).
Flow: FetchRequest + committed coverage -> merge/clip -> find gaps
      -> apply RANGE, FULL or REFRESH rules -> FetchPlan.
Coverage requires validated, committed intervals, not observation dates alone.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable

from backend.models import DateRange, FetchMode, FetchPlan, FetchRequest


class PlanningError(ValueError):
    """Invalid or unsupported fetch-planning parameters."""


def merge_ranges(ranges: Iterable[DateRange]) -> tuple[DateRange, ...]:
    """Return sorted, merged inclusive intervals without modifying inputs."""
    items = tuple(ranges)
    for item in items:
        if not isinstance(item, DateRange):
            raise TypeError("All coverage entries must be DateRange")
    if not items:
        return ()

    ordered = sorted(items, key=lambda item: (item.start, item.end))
    merged: list[DateRange] = []
    ordered_iter = iter(ordered)
    current = next(ordered_iter)
    start, end = current.start, current.end
    end_ordinal = end.toordinal()
    combined = False

    for item in ordered_iter:
        # Ordinals avoid overflow when end == date.max.
        if item.start.toordinal() <= end_ordinal + 1:
            combined = True
            if item.end > end:
                end = item.end
                end_ordinal = end.toordinal()
        else:
            # Create only one DateRange per merged group, not per overlap.
            merged.append(DateRange(start, end) if combined else current)
            current = item
            start, end = item.start, item.end
            end_ordinal = end.toordinal()
            combined = False

    merged.append(DateRange(start, end) if combined else current)
    return tuple(merged)


def _intersect_merged(
    target: DateRange, merged_coverage: tuple[DateRange, ...]
) -> tuple[DateRange, ...]:
    """Clip already sorted, nonadjacent coverage; clipping cannot add overlaps."""
    if not isinstance(target, DateRange):
        raise TypeError("target must be DateRange")
    intersections: list[DateRange] = []
    target_start, target_end = target.start, target.end
    for item in merged_coverage:
        start = max(target_start, item.start)
        end = min(target_end, item.end)
        if start <= end:
            intersections.append(DateRange(start, end))
    return tuple(intersections)


def intersect_ranges(
    target: DateRange, ranges: Iterable[DateRange]
) -> tuple[DateRange, ...]:
    """Return merged coverage clipped to target."""
    if not isinstance(target, DateRange):
        raise TypeError("target must be DateRange")
    return _intersect_merged(target, merge_ranges(ranges))


def _subtract_covered(
    target: DateRange, covered: tuple[DateRange, ...]
) -> tuple[DateRange, ...]:
    """Subtract coverage already sorted, merged and clipped to target."""
    missing: list[DateRange] = []
    cursor = target.start.toordinal()
    target_end = target.end.toordinal()

    for item in covered:
        start = item.start.toordinal()
        end = item.end.toordinal()
        if cursor < start:
            missing.append(DateRange(date.fromordinal(cursor), date.fromordinal(start - 1)))
        cursor = max(cursor, end + 1)
        if cursor > target_end:
            break

    if cursor <= target_end:
        missing.append(DateRange(date.fromordinal(cursor), target.end))
    return tuple(missing)


def subtract_ranges(
    target: DateRange, coverage: Iterable[DateRange]
) -> tuple[DateRange, ...]:
    """Return uncovered inclusive intervals in ascending order."""
    if not isinstance(target, DateRange):
        raise TypeError("target must be DateRange")
    return _subtract_covered(target, intersect_ranges(target, coverage))


def subtract_years(value: date, years: int) -> date:
    """Subtract calendar years; adjust February 29 to February 28."""
    if type(value) is not date:
        raise TypeError("value must be a date")
    if isinstance(years, bool) or not isinstance(years, int) or years < 0:
        raise ValueError("years must be a non-negative integer")

    target_year = value.year - years
    if target_year < 1:
        return date.min
    try:
        return value.replace(year=target_year)
    except ValueError:
        return date(target_year, 2, 28)


class FetchPlanner:
    """Create fetch plans; only a successful storage commit establishes coverage.

    RANGE fetches missing requested dates; FULL fetches missing history from
    the provider's earliest date; REFRESH also rechecks recent revisions.
    Force overrides the resulting fetch intervals in every mode.
    """

    def __init__(self, *, revision_years: int = 5) -> None:
        if (
            isinstance(revision_years, bool)
            or not isinstance(revision_years, int)
            or revision_years < 0
        ):
            raise ValueError("revision_years must be a non-negative integer")
        self.revision_years = revision_years

    @staticmethod
    def _validate_as_of(value: date) -> None:
        if type(value) is not date:
            raise TypeError("as_of_date must be a date")

    @staticmethod
    def _validate_coverage(
        coverage: Iterable[DateRange],
    ) -> tuple[DateRange, ...]:
        if coverage is None:
            raise TypeError("coverage cannot be None")
        return merge_ranges(coverage)

    def plan(
        self,
        request: FetchRequest,
        coverage: Iterable[DateRange],
        *,
        as_of_date: date,
        earliest_available_date: date | None = None,
    ) -> FetchPlan:
        """Return target, cached and fetch intervals without performing I/O."""
        if not isinstance(request, FetchRequest):
            raise TypeError("request must be FetchRequest")
        self._validate_as_of(as_of_date)
        if earliest_available_date is not None and type(earliest_available_date) is not date:
            raise TypeError("earliest_available_date must be a date")

        verified_coverage = self._validate_coverage(coverage)
        mode = request.mode

        if mode is FetchMode.FULL:
            if earliest_available_date is None:
                raise PlanningError("FULL mode requires earliest_available_date")
            if earliest_available_date > as_of_date:
                raise PlanningError("Earliest available date is after as_of_date")
            target = DateRange(earliest_available_date, as_of_date)
        else:
            target = request.date_range
            if target.start > as_of_date:
                raise PlanningError("Requested range begins in the future")
            if target.end > as_of_date:
                target = DateRange(target.start, as_of_date)

        # Coverage is already merged: do not sort or merge it again.
        cached_ranges = _intersect_merged(target, verified_coverage)

        if mode is FetchMode.FULL or mode is FetchMode.RANGE:
            if request.force:
                fetch_ranges = (target,)
                reason = (
                    "Forced full historical backfill"
                    if mode is FetchMode.FULL
                    else "Forced range revalidation"
                )
            else:
                fetch_ranges = _subtract_covered(target, cached_ranges)
                if mode is FetchMode.FULL:
                    reason = (
                        "Full backfill of uncovered history"
                        if fetch_ranges else "Full history already covered"
                    )
                else:
                    reason = (
                        "Fetch uncovered requested ranges"
                        if fetch_ranges else "Requested range fully cached"
                    )

        elif mode is FetchMode.REFRESH:
            missing = _subtract_covered(target, cached_ranges)
            revision_start = subtract_years(as_of_date, self.revision_years)
            overlap_start = max(target.start, revision_start)
            ranges_to_fetch = list(missing)
            if overlap_start <= target.end:
                ranges_to_fetch.append(DateRange(overlap_start, target.end))

            if request.force:
                fetch_ranges = (target,)
                reason = "Forced refresh of requested range"
            else:
                fetch_ranges = merge_ranges(ranges_to_fetch)
                reason = (
                    "Fetch missing history and refresh recent revisions"
                    if fetch_ranges else "No missing or refreshable range"
                )
        else:
            raise PlanningError(f"Unsupported fetch mode: {mode}")

        return FetchPlan(
            request=request,
            fetch_ranges=fetch_ranges,
            cached_ranges=cached_ranges,
            reason=reason,
        )
