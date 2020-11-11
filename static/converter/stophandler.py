from functools import lru_cache
from logging import getLogger
from os.path import join
from typing import Any, Callable, Dict, Optional, List, Sequence, Set, Tuple
import requests
import json
import csv

from ..const import PROPER_STOP_NAMES, ACTIVE_RAIL_STATIONS, HEADERS, \
    GIST_MISSING_STOPS, GIST_RAIL_PLATFORMS
from ..parser.dataobj import ZTMStop, ZTMStopGroup


"""
Module reposible for handling handling stop data.

Converts ZTM group-stake hierarchy to GTFS representations of real-life structures.
Fills missing data from external gists (see ../const.py); manages which stops to export,
and all that kind of jazz.
"""


def normalize_stop_name(name: str) -> str:
    """Attempts to fix stop names provided by ZTM"""
    # add .title() if ZTM provides names in ALL-UPPER CASE again
    name = name.replace(".", ". ")      \
               .replace("-", " - ")     \
               .replace("  ", " ")      \
               .replace("al.", "Al.")   \
               .replace("pl.", "Pl.")   \
               .replace("os.", "Os.")   \
               .replace("ks.", "Ks.")   \
               .replace("Ak ", "AK ")   \
               .replace("Ch ", "CH ")   \
               .replace("gen.", "Gen.") \
               .replace("rondo ", "Rondo ") \
               .rstrip()

    return name


def should_town_be_added_to_name(group: ZTMStopGroup) -> bool:
    """Checks whether town name should be added to the stop name"""
    # List of conditions that, if true, mean town name shouldn't be added
    dont_add_conditions: Set[Callable[[ZTMStopGroup], bool]] = {
        lambda g: g.town_code == "--",  # Stops in Warsaw
        lambda g: g.id[1:3] in {"90", "91", "92"},  # Railway stations
        lambda g: "PKP" in g.name,  # Stops near train stations
        lambda g: "WKD" in g.name,  # Stops near WKD stations
        lambda g: g.town.casefold() in g.name.casefold(),  # Town name is already in stop name

        # Any part of town name is already in the stop name
        lambda g: any(part in g.name.casefold() for part in g.town.casefold().split(" "))
    }

    # Check if all dont_add_conditions fail
    return not any(rule(group) for rule in dont_add_conditions)


def avg_position(stops: Sequence[ZTMStop]) -> Optional[Tuple[float, float]]:
    """Returns the average position of all stops"""
    lats = (i.lat for i in stops if i.lon is not None)
    lons = (i.lon for i in stops if i.lon is not None)
    count = len(stops)

    if count < 1:
        return None

    return sum(lats) / count, sum(lons) / count


@lru_cache(maxsize=None)
def get_missing_stops() -> Dict[str, Tuple[float, float]]:
    """Gets positions of stops from external gist, as ZTM sometimes omits stop coordinates"""
    with requests.get(GIST_MISSING_STOPS) as req:
        req.raise_for_status()
        return req.json()


@lru_cache(maxsize=None)
def get_rail_platforms() -> Dict[str, Dict[str, Any]]:
    """Gets info about railway stations from external gist"""
    with requests.get(GIST_RAIL_PLATFORMS) as req:
        req.raise_for_status()
        return req.json()


class StopHandler:
    def __init__(self, version: str) -> None:
        self.logger = getLogger(f"WarsawGTFS.{version}.StopHandler")

        # Stop data
        self.names = PROPER_STOP_NAMES.copy()
        self.data: Dict[str, Dict[str, Any]] = {}
        self.parents: Dict[str, str] = {}
        self.zones: Dict[str, str] = {}

        # Invalid stop data
        self.invalid: Dict[str, ZTMStop] = {}
        self.change: Dict[str, Optional[str]] = {}

        # Used stops
        self.used_invalid: Set[str] = set()
        self.used: Set[str] = set()

        # External data
        self.missing_stops: Dict[str, Tuple[float, float]] = {}
        self.rail_platforms: Dict[str, Dict[str, Any]] = {}
        self._load_external()

    def _load_external(self) -> None:
        """Loads data from external gists"""
        self.logger.info("Loading data from external gists")
        self.missing_stops = get_missing_stops()
        self.rail_platforms = get_rail_platforms()

    @staticmethod
    def _match_virtual(virt: ZTMStop, stakes: Sequence[ZTMStop]) -> Optional[str]:
        """Try to find a normal stake corresponding to given virtual stake"""
        # Find normal stakes with matching position
        if virt.lat is not None and virt.lon is not None:
            with_same_pos = [i.id for i in stakes if i.code[0] != "8"
                             and i.lat == virt.lat and i.lon == virt.lon]
        else:
            with_same_pos = []

        # Find normal stakes with matching code
        with_same_code = [i.id for i in stakes if i.code[0] != "8"
                          and i.code[1] == virt.code[1]]

        # Special Case: Metro Młociny 88 → Metro Młociny 28
        if virt.id == "605988" and "605928" in with_same_code:
            return "605928"

        # Matched stakes with the same position
        if with_same_pos:
            return with_same_pos[0]

        # Matched stakes with the same code
        elif with_same_code:
            return with_same_code[0]

        # Unable to find a match
        else:
            return None

    def _find_missing_positions(self, stops: List[ZTMStop]) -> None:
        """Matches data from missing_stops to a list of loaded ZTMStops."""
        for idx, stop in enumerate(stops):

            if stop.lat is None or stop.lon is None:
                missing_pos = self.missing_stops.get(stop.id)

                if missing_pos:
                    stops[idx].lat, stops[idx].lon = missing_pos

    def _load_normal_group(self, group_name: str, stops: List[ZTMStop]):
        """Saves info about normal stop group"""
        for stop in stops:

            # Fix virtual stops
            if stop.code[0] == "8":
                change_to = self._match_virtual(stop, stops)

                if change_to is not None:
                    self.change[stop.id] = change_to

                else:
                    self.invalid[stop.id] = stop

                continue

            # Handle undefined stop positions
            if stop.lat is None or stop.lon is None:
                self.invalid[stop.id] = stop
                continue

            # Save stake into self.data
            self.data[stop.id] = {
                "stop_id": stop.id,
                "stop_name": group_name + " " + stop.code,
                "stop_lat": stop.lat,
                "stop_lon": stop.lon,
                "wheelchair_boarding": stop.wheelchair,
            }

    def _load_railway_group(self, group_id: str, group_name: str, virt_stops: List[ZTMStop]):
        """Saves data about a stop group representing a railway station"""
        # Nop KM & WKD stations
        if group_id not in ACTIVE_RAIL_STATIONS:
            for i in virt_stops:
                self.change[i.id] = None
            return

        # Load station info
        station_data = self.rail_platforms.get(group_id, {})

        # If this station is not in rail_platforms, average all stake positions
        # In order to calculate an approx. position of the station
        if not station_data:
            avg_pos = avg_position(virt_stops)

            if avg_pos:
                station_lat, station_lon = avg_pos

            # Halt processing if we have no geographical data
            else:
                for i in virt_stops:
                    self.change[i.id] = None
                return

        # Otherwise get the position from rail_platforms data
        else:
            station_lat, station_lon = map(float, station_data["pos"].split(","))
            group_name = station_data["name"]

        # Map every stake into one node
        if (not station_data) or station_data["oneplatform"]:

            self.data[group_id] = {
                "stop_id": group_id,
                "stop_name": group_name,
                "stop_lat": station_lat,
                "stop_lon": station_lon,
                "zone_id": station_data.get("zone_id", ""),
                "stop_IBNR": station_data.get("ibnr_code", ""),
                "stop_PKPPLK": station_data.get("pkpplk_code", ""),
                "wheelchair_boarding": station_data.get("wheelchair", "0"),
            }

            for i in virt_stops:
                self.change[i.id] = group_id

        # Process multi-platform station
        else:
            # Add hub entry
            self.data[group_id] = {
                "stop_id": group_id,
                "stop_name": group_name,
                "stop_lat": station_lat,
                "stop_lon": station_lon,
                "location_type": "1",
                "parent_station": "",
                "zone_id": station_data.get("zone_id", ""),
                "stop_IBNR": station_data.get("ibnr_code", ""),
                "stop_PKPPLK": station_data.get("pkpplk_code", ""),
                "wheelchair_boarding": station_data.get("wheelchair", "0"),
            }

            # Platforms
            for platform_id, platform_pos in station_data["platforms"].items():
                platform_lat, platform_lon = map(float, platform_pos.split(","))
                platform_code = platform_id.split("p")[1]
                platform_name = f"{group_name} peron {platform_code}"

                # Add platform entry
                self.data[platform_id] = {
                    "stop_id": platform_id,
                    "stop_name": platform_name,
                    "stop_lat": platform_lat,
                    "stop_lon": platform_lon,
                    "location_type": "0",
                    "parent_station": group_id,
                    "zone_id": station_data.get("zone_id", ""),
                    "stop_IBNR": station_data.get("ibnr_code", ""),
                    "stop_PKPPLK": station_data.get("pkpplk_code", ""),
                    "wheelchair_boarding": station_data.get("wheelchair", "0"),
                }

                # Add to self.parents
                self.parents[platform_id] = group_id

            # Stops → Platforms
            for stop in virt_stops:

                # Defined stake in rail_platforms
                if stop.id in station_data["stops"]:
                    self.change[stop.id] = station_data["stops"][stop.id]

                # Unknown stake
                elif stop.id not in {"491303", "491304"}:
                    self.logger.warn(
                        f"No platform defined for railway PR entry {group_name} {stop.id}"
                    )

    def load_group(self, group: ZTMStopGroup, stops: List[ZTMStop]) -> None:
        """Loads info about stops of a specific group"""
        # Fix name "Kampinoski Pn" town name
        if group.town == "Kampinoski Pn":
            group.town = "Kampinoski PN"

        # Fix group name
        group.name = normalize_stop_name(group.name)

        # Add town name to stop name & save name to self.names
        if (fixed_name := self.names.get(group.id)):
            group.name = fixed_name

        elif should_town_be_added_to_name(group):
            group.name = group.town + " " + group.name
            self.names[group.id] = group.name

        else:
            self.names[group.id] = group.name

        # Add missing positions to stakes
        self._find_missing_positions(stops)

        # Parse stakes
        if group.id[1:3] in {"90", "91", "92"}:
            self._load_railway_group(group.id, group.name, stops)

        else:
            self._load_normal_group(group.name, stops)

    def get_id(self, original_id: Optional[str]) -> Optional[str]:
        """
        Should the stop_id be changed, provide the correct stop_id.
        If given stop_id has its position undefined returns None.
        """
        if original_id is None:
            return None

        valid_id = self.change.get(original_id, original_id)

        if valid_id is None or valid_id in self.invalid:
            return None

        elif valid_id in self.invalid:
            self.used_invalid.add(valid_id)
            return None

        else:
            return valid_id

    def use(self, stop_id: str) -> None:
        """Mark provided GTFS stop_id as used"""
        # Check if this stop belogins to a larger group
        parent_id = self.parents.get(stop_id)

        # Mark the parent as used
        if parent_id is not None:
            self.used.add(parent_id)

        self.used.add(stop_id)

    def zone_set(self, group_id: str, zone_id: str) -> None:
        """Saves assigned zone for a particular stop group"""
        current_zone = self.zones.get(group_id)

        # Zone has not changed: skip
        if current_zone == zone_id:
            return

        if current_zone is None:
            self.zones[group_id] = zone_id

        # Boundary stops shouldn't generate a zone conflict warning
        elif current_zone == "1/2" or zone_id == "1/2":
            self.zones[group_id] = "1/2"

        else:
            self.logger.warn(
                f"Stop group {group_id} has a zone confict: it was set to {current_zone!r}, "
                f"but now it needs to be set to {zone_id!r}"
            )

            self.zones[group_id] = "1/2"

    def export(self, gtfs_dir: str):
        """Exports all used stops (and their parents) to {gtfs_dir}/stops.txt"""
        # Export all stops
        self.logger.info("Exporting stops")
        with open(join(gtfs_dir, "stops.txt"), mode="w", encoding="utf8", newline="") as f:
            writer = csv.DictWriter(f, HEADERS["stops.txt"])
            writer.writeheader()

            for stop_id, stop_data in self.data.items():
                # Check if stop was used or (is a part of station and not a stop-chlid)
                if stop_id in self.used or (stop_data.get("parent_station") in self.used
                                            and stop_data.get("location_type") != "1"):

                    # Set the zone_id
                    if not stop_data.get("zone_id"):
                        zone_id = self.zones.get(stop_id[:4])

                        if zone_id is None:
                            self.logger.warn(
                                f"Stop group {stop_id[:4]} has no zone_id assigned (using '1/2')"
                            )
                            zone_id = "1/2"

                        stop_data["zone_id"] = zone_id

                    writer.writerow(stop_data)

        # Calculate unused stos from missing
        unused_missing = set(self.missing_stops.keys()).difference(self.used_invalid)

        # Dump missing stops info
        self.logger.info("Exporting missing_stops.json")
        with open("missing_stops.json", "w") as f:
            json.dump(
                {"missing": sorted(self.used_invalid), "unused": sorted(unused_missing)},
                f,
                indent=2
            )