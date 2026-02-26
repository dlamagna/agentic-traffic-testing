"""
MCP server exposing simple, synthetic mapping/geocoding tools.

This server avoids real external APIs so all traffic stays local
to the testbed while still giving agents something map-like to use.
"""

from __future__ import annotations

import math

from fastmcp import FastMCP


server = FastMCP("maps-server")


_LOCATIONS = {
    "new york": {"lat": 40.7128, "lng": -74.0060, "city": "New York", "country": "USA"},
    "london": {"lat": 51.5074, "lng": -0.1278, "city": "London", "country": "UK"},
    "tokyo": {"lat": 35.6762, "lng": 139.6503, "city": "Tokyo", "country": "Japan"},
    "paris": {"lat": 48.8566, "lng": 2.3522, "city": "Paris", "country": "France"},
}


def _resolve_location(address: str) -> dict:
    """Internal helper to resolve an address into the synthetic database."""
    query = address.lower()
    for key, info in _LOCATIONS.items():
        if key in query:
            return {
                "address": address,
                "coordinates": {
                    "latitude": info["lat"],
                    "longitude": info["lng"],
                },
                "city": info["city"],
                "country": info["country"],
                "found": True,
            }

    return {
        "address": address,
        "found": False,
        "error": "Location not found in local maps database.",
    }


@server.tool()
def geocode_location(address: str) -> dict:
    """
    Map a simple place name to synthetic coordinates.

    This is intentionally fuzzy and matches by substring.
    """
    return _resolve_location(address)


@server.tool()
def calculate_distance(location1: str, location2: str) -> dict:
    """
    Calculate great-circle distance between two known locations.
    """
    loc1 = _resolve_location(location1)
    loc2 = _resolve_location(location2)

    if not (loc1.get("found") and loc2.get("found")):
        return {"error": "One or both locations could not be resolved."}

    lat1 = math.radians(loc1["coordinates"]["latitude"])
    lon1 = math.radians(loc1["coordinates"]["longitude"])
    lat2 = math.radians(loc2["coordinates"]["latitude"])
    lon2 = math.radians(loc2["coordinates"]["longitude"])

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    r_km = 6371.0
    distance_km = r_km * c

    return {
        "from": location1,
        "to": location2,
        "distance_km": round(distance_km, 2),
        "distance_miles": round(distance_km * 0.621371, 2),
    }


@server.resource("resource://maps/known-locations")
def list_known_locations() -> dict:
    """Return the small catalog of synthetic locations."""
    return {
        "locations": [
            {
                "name": data["city"],
                "country": data["country"],
                "coordinates": {"lat": data["lat"], "lng": data["lng"]},
            }
            for data in _LOCATIONS.values()
        ]
    }


if __name__ == "__main__":
    server.run()

