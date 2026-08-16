"""Automatic seasonal provider keyed to the nearest major US city.

Pure module: no Home Assistant imports. The caller supplies the system's
latitude/longitude (Home Assistant's configured home location) and the
provider scales each zone's base runtime by a per-month percentage taken
from a bundled curve for the nearest major US city.

The curves are percent-of-peak-month reference evapotranspiration (ETo)
shapes derived from 1961–1990-style climate normals for each city's region
(the same vintage the EPA WaterSense water-budget tool uses via the IWMI
World Water and Climate Atlas; regional monthly ETo shapes as published by
e.g. CIMIS for California, TexasET for Texas, and NWS/NOAA evaporation
atlases). They are deliberately coarse guidance, not a proprietary
reproduction: the peak month is always 100%, and a zone's base runtime is
interpreted as its PEAK-SEASON requirement. Every run's math (city, month,
percentage) is spelled out in the adjustment explanation, and users who
want exact local control can switch the program to the monthly-curve
provider and edit the twelve values directly.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import NamedTuple

from ..models import AdjustmentResult, Program, ProgramOccurrence, ZoneProfile
from .base import effective_base_minutes, percent_result


class SeasonalCity(NamedTuple):
    """One bundled reference city."""

    name: str
    latitude: float
    longitude: float
    monthly_percent: tuple[int, ...]  # Jan..Dec, percent of peak month


# Regional percent-of-peak ETo shapes (Jan..Dec). Peak month = 100.
_DESERT_SW = (30, 40, 55, 75, 90, 100, 100, 90, 80, 60, 40, 30)
_SOCAL_COAST = (45, 50, 60, 70, 80, 90, 100, 100, 90, 70, 55, 45)
_CALIF_VALLEY = (25, 35, 50, 65, 80, 95, 100, 95, 80, 60, 35, 25)
_MARINE_NW = (15, 25, 40, 55, 75, 90, 100, 95, 70, 40, 20, 15)
_MOUNTAIN_WEST = (20, 30, 45, 65, 80, 95, 100, 95, 75, 50, 30, 20)
_SOUTHERN_PLAINS = (30, 40, 55, 70, 85, 95, 100, 100, 85, 65, 45, 35)
_SOUTHEAST = (25, 35, 50, 65, 80, 95, 100, 95, 80, 60, 40, 30)
_FLORIDA = (55, 65, 75, 85, 95, 100, 100, 95, 90, 80, 65, 55)
_MIDWEST = (15, 20, 35, 55, 75, 90, 100, 95, 75, 55, 30, 15)
_NORTHEAST = (20, 25, 40, 55, 75, 90, 100, 95, 75, 55, 35, 20)
_TROPICAL = (70, 75, 85, 90, 95, 100, 100, 100, 95, 85, 75, 70)
_SUBARCTIC = (10, 15, 30, 55, 80, 100, 100, 85, 60, 30, 15, 10)

CITIES: tuple[SeasonalCity, ...] = (
    SeasonalCity("Phoenix, AZ", 33.45, -112.07, _DESERT_SW),
    SeasonalCity("Tucson, AZ", 32.22, -110.97, _DESERT_SW),
    SeasonalCity("Las Vegas, NV", 36.17, -115.14, _DESERT_SW),
    SeasonalCity("El Paso, TX", 31.76, -106.49, _DESERT_SW),
    SeasonalCity("Los Angeles, CA", 34.05, -118.24, _SOCAL_COAST),
    SeasonalCity("San Diego, CA", 32.72, -117.16, _SOCAL_COAST),
    SeasonalCity("San Francisco, CA", 37.77, -122.42, _CALIF_VALLEY),
    SeasonalCity("Sacramento, CA", 38.58, -121.49, _CALIF_VALLEY),
    SeasonalCity("Fresno, CA", 36.74, -119.79, _CALIF_VALLEY),
    SeasonalCity("Seattle, WA", 47.61, -122.33, _MARINE_NW),
    SeasonalCity("Portland, OR", 45.52, -122.68, _MARINE_NW),
    SeasonalCity("Spokane, WA", 47.66, -117.43, _MOUNTAIN_WEST),
    SeasonalCity("Boise, ID", 43.62, -116.20, _MOUNTAIN_WEST),
    SeasonalCity("Salt Lake City, UT", 40.76, -111.89, _MOUNTAIN_WEST),
    SeasonalCity("Denver, CO", 39.74, -104.99, _MOUNTAIN_WEST),
    SeasonalCity("Albuquerque, NM", 35.08, -106.65, _MOUNTAIN_WEST),
    SeasonalCity("Billings, MT", 45.79, -108.50, _MOUNTAIN_WEST),
    SeasonalCity("Dallas, TX", 32.78, -96.80, _SOUTHERN_PLAINS),
    SeasonalCity("Austin, TX", 30.27, -97.74, _SOUTHERN_PLAINS),
    SeasonalCity("San Antonio, TX", 29.42, -98.49, _SOUTHERN_PLAINS),
    SeasonalCity("Houston, TX", 29.76, -95.37, _SOUTHERN_PLAINS),
    SeasonalCity("Oklahoma City, OK", 35.47, -97.52, _SOUTHERN_PLAINS),
    SeasonalCity("Wichita, KS", 37.69, -97.34, _SOUTHERN_PLAINS),
    SeasonalCity("Atlanta, GA", 33.75, -84.39, _SOUTHEAST),
    SeasonalCity("Charlotte, NC", 35.23, -80.84, _SOUTHEAST),
    SeasonalCity("Nashville, TN", 36.16, -86.78, _SOUTHEAST),
    SeasonalCity("Memphis, TN", 35.15, -90.05, _SOUTHEAST),
    SeasonalCity("Birmingham, AL", 33.52, -86.80, _SOUTHEAST),
    SeasonalCity("New Orleans, LA", 29.95, -90.07, _SOUTHEAST),
    SeasonalCity("Richmond, VA", 37.54, -77.44, _SOUTHEAST),
    SeasonalCity("Miami, FL", 25.76, -80.19, _FLORIDA),
    SeasonalCity("Tampa, FL", 27.95, -82.46, _FLORIDA),
    SeasonalCity("Orlando, FL", 28.54, -81.38, _FLORIDA),
    SeasonalCity("Jacksonville, FL", 30.33, -81.66, _FLORIDA),
    SeasonalCity("Chicago, IL", 41.88, -87.63, _MIDWEST),
    SeasonalCity("Minneapolis, MN", 44.98, -93.27, _MIDWEST),
    SeasonalCity("Detroit, MI", 42.33, -83.05, _MIDWEST),
    SeasonalCity("Milwaukee, WI", 43.04, -87.91, _MIDWEST),
    SeasonalCity("Kansas City, MO", 39.10, -94.58, _MIDWEST),
    SeasonalCity("St. Louis, MO", 38.63, -90.20, _MIDWEST),
    SeasonalCity("Indianapolis, IN", 39.77, -86.16, _MIDWEST),
    SeasonalCity("Columbus, OH", 39.96, -83.00, _MIDWEST),
    SeasonalCity("Omaha, NE", 41.26, -95.93, _MIDWEST),
    SeasonalCity("New York, NY", 40.71, -74.01, _NORTHEAST),
    SeasonalCity("Boston, MA", 42.36, -71.06, _NORTHEAST),
    SeasonalCity("Philadelphia, PA", 39.95, -75.17, _NORTHEAST),
    SeasonalCity("Washington, DC", 38.91, -77.04, _NORTHEAST),
    SeasonalCity("Pittsburgh, PA", 40.44, -79.99, _NORTHEAST),
    SeasonalCity("Baltimore, MD", 39.29, -76.61, _NORTHEAST),
    SeasonalCity("Honolulu, HI", 21.31, -157.86, _TROPICAL),
    SeasonalCity("Anchorage, AK", 61.22, -149.90, _SUBARCTIC),
)

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

_EARTH_RADIUS_MILES = 3958.8


def _distance_miles(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance (haversine)."""
    rlat1, rlon1, rlat2, rlon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def nearest_city(
    latitude: float, longitude: float
) -> tuple[SeasonalCity, float]:
    """Return the closest bundled city and its distance in miles."""
    best = min(
        CITIES,
        key=lambda city: _distance_miles(
            latitude, longitude, city.latitude, city.longitude
        ),
    )
    return best, _distance_miles(
        latitude, longitude, best.latitude, best.longitude
    )


class SeasonalAutoProvider:
    """Monthly percent-of-peak curve chosen by the system's location."""

    def __init__(self, location: tuple[float, float] | None) -> None:
        self._location = location

    async def async_calculate(
        self,
        zone: ZoneProfile,
        program: Program,
        occurrence: ProgramOccurrence,
    ) -> AdjustmentResult:
        base = effective_base_minutes(zone, program)
        if self._location is None:
            return percent_result(
                base=base,
                percent=Decimal(100),
                explanation=[
                    "No home location configured in Home Assistant.",
                    "Fell back to 100% (base runtime unchanged).",
                ],
                stale_inputs=("home_location",),
            )
        city, miles = nearest_city(*self._location)
        month = occurrence.scheduled_start_local.month
        percent = Decimal(city.monthly_percent[month - 1])
        return percent_result(
            base=base,
            percent=percent,
            explanation=[
                f"Base runtime {base} min × {percent}% "
                f"({_MONTHS[month - 1]} percent-of-peak for {city.name}, "
                f"nearest bundled city, {round(miles)} mi away).",
                "Base runtime is treated as the zone's peak-season "
                "requirement; curves follow published regional ETo "
                "climate normals.",
            ],
        )
