"""Report known baseline failures explicitly; fail on any changed failure set."""
import sys
import xml.etree.ElementTree as ET

expected = {
    "test_minimum_pilot_current_floor",
    "test_priority_weighted_allocation",
    "test_station_overrides",
}

def failures(path):
    root = ET.parse(path).getroot()
    assert not root.findall(".//error"), "Allocator collection/runtime error"
    return {case.attrib["name"] for case in root.findall(".//testcase")
            if case.find("failure") is not None}

assert failures(sys.argv[1]) == expected, "Baseline changed; review required"
assert failures(sys.argv[2]) == expected, "Allocator regression or fix; review required"
print("BASELINE DEBT: the same three allocator assertions fail on both pinned baseline and PR.")
