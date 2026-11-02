"""Dependency-free tests: run with python -m unittest discover -s plugins/nightdrive/tests -v.

The production module is loaded directly so these tests exercise Fio game
physics without pulling in the editor, Qt, GL, NumPy or the plugin bootstrap.
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import sys
import unittest

GAME = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("nightdrive_vehicle_unit", GAME / "vehicle.py")
vehicle = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = vehicle
SPEC.loader.exec_module(vehicle)


class VehicleTests(unittest.TestCase):
    def test_initially_still(self):
        car = vehicle.Vehicle(0.0, 0.0)
        car.step(1 / 60, 0, 1)
        self.assertEqual(car.speed, 0)
        self.assertEqual(car.angle, 0)

    def test_accelerates_and_moves_in_fio_forward_axis(self):
        car = vehicle.Vehicle(0.0, 0.0)
        for _ in range(60):
            car.step(1 / 60, 1, 0)
        self.assertGreater(car.speed, 200)
        self.assertGreater(car.z, 80)
        self.assertAlmostEqual(car.x, 0)

    def test_forward_speed_limited(self):
        car = vehicle.Vehicle(0.0, 0.0)
        for _ in range(200):
            car.step(1 / 60, 1, 0)
        self.assertLessEqual(car.speed, vehicle.MAX_FORWARD)

    def test_reverses_and_respects_reverse_speed_limit(self):
        car = vehicle.Vehicle(0.0, 0.0)
        for _ in range(160):
            car.step(1 / 60, -1, 0)
        self.assertLess(car.z, 0)
        self.assertGreaterEqual(car.speed, -vehicle.MAX_REVERSE)

    def test_both_turn_directions(self):
        left = vehicle.Vehicle(0.0, 0.0, speed=180)
        right = vehicle.Vehicle(0.0, 0.0, speed=180)
        left.step(0.05, 0, -1)
        right.step(0.05, 0, 1)
        self.assertLess(left.angle, 0)
        self.assertGreater(right.angle, 0)

    def test_reverse_steer_direction(self):
        car = vehicle.Vehicle(0, 0, speed=-100)
        car.step(0.04, 0, 1)
        self.assertLess(car.angle, 0)

    def test_coasting_and_handbrake(self):
        coast = vehicle.Vehicle(0, 0, speed=200)
        drift = vehicle.Vehicle(0, 0, speed=200)
        coast.step(.05, 0, 0)
        drift.step(.05, 0, 0, handbrake=True)
        self.assertLess(drift.speed, coast.speed)

    def test_dt_is_clamped(self):
        a = vehicle.Vehicle(0, 0)
        b = vehicle.Vehicle(0, 0)
        a.step(999, 1, 0)
        b.step(.1, 1, 0)
        self.assertAlmostEqual(a.speed, b.speed)
        self.assertAlmostEqual(a.z, b.z)

    def test_collision_and_no_tunnelling(self):
        car = vehicle.Vehicle(0, 0, speed=400)
        # A thin obstacle is still hit during an accelerated frame.
        impacted = car.step(.1, 1, 0, collides=lambda x, z, r:
                            vehicle.circle_hits_rect(x, z, r, (-60, 42, 60, 47)))
        self.assertTrue(impacted)
        self.assertLess(car.z, 42)
        self.assertEqual(car.impacts, 1)
        self.assertLess(car.speed, 0)

    def test_collision_does_not_happen_when_clear(self):
        car = vehicle.Vehicle(0, 0, speed=100)
        self.assertFalse(car.step(.02, 1, 0, collides=lambda x,z,r: False))
        self.assertEqual(car.impacts, 0)

    def test_circle_rectangle_edge(self):
        rect = (10, 10, 30, 30)
        self.assertTrue(vehicle.circle_hits_rect(8, 20, 3, rect))
        self.assertFalse(vehicle.circle_hits_rect(0, 0, 3, rect))
        self.assertTrue(vehicle.circle_hits_rect(20, 20, 1, rect))

    def test_angle_shortest_turn(self):
        delta = vehicle.angle_delta(-math.pi + .1, math.pi - .1)
        self.assertAlmostEqual(delta, .2)

    def test_traffic_follows_route(self):
        route = ((0, 0), (300, 0), (300, 300), (0, 300))
        ai = vehicle.Traffic(vehicle.Vehicle(0, 0, math.pi / 2),
                             route, 140)
        for _ in range(80):
            ai.update(1 / 60)
        self.assertGreater(ai.vehicle.x, 100)
        self.assertGreaterEqual(ai.next_waypoint, 1)
        self.assertLess(ai.next_waypoint, len(route))

    def test_traffic_empty_route_safe(self):
        ai = vehicle.Traffic(vehicle.Vehicle(0, 0), tuple(), 100)
        ai.update(1 / 60)
        self.assertEqual((ai.vehicle.x, ai.vehicle.z), (0, 0))


class LevelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.level = json.loads((GAME / "NightDrive_City.json").read_text())

    def test_fio_map_version(self):
        self.assertEqual(self.level["version"], 3)

    def test_has_physical_city_geometry(self):
        brushes = self.level["brushes"]
        self.assertGreater(len(brushes), 350)
        buildings = [b for b in brushes if b.get("nightdrive_solid")]
        self.assertEqual(len(buildings), 36)

    def test_ids_are_unique_and_records_well_formed(self):
        ids = [b["id"] for b in self.level["brushes"]]
        ids += [t["properties"]["id"] for t in self.level["things"]]
        self.assertEqual(len(ids), len(set(ids)))
        for b in self.level["brushes"]:
            self.assertEqual(len(b["pos"]), 3)
            self.assertEqual(len(b["size"]), 3)
            self.assertTrue(all(s > 0 for s in b["size"]))
            self.assertIn("top", b["textures"])

    def test_game_entities(self):
        things = self.level["things"]
        roles = [t["properties"].get("drive_role") for t in things]
        self.assertEqual(roles.count("player_vehicle"), 1)
        self.assertEqual(roles.count("traffic"), 12)
        self.assertEqual(roles.count("delivery"), 5)
        self.assertEqual(sum(t["type"] == "playerstart" for t in things), 1)
        self.assertEqual(sum(t["type"] == "light" for t in things), 16)

    def test_routes_have_valid_waypoints(self):
        for t in self.level["things"]:
            p = t["properties"]
            if p.get("drive_role") != "traffic":
                continue
            route = p["drive_route"]
            self.assertEqual(len(route), 4)
            self.assertTrue(0 <= p["drive_next_waypoint"] < len(route))
            self.assertGreater(p["drive_speed"], 0)
            self.assertTrue(all(len(x) == 2 for x in route))

    def test_vehicle_assets_exist(self):
        for t in self.level["things"]:
            if t["type"] != "prop":
                continue
            model = t["properties"].get("model_path")
            self.assertIsNotNone(model)
            self.assertTrue((GAME.parents[1] / model).is_file(), model)

    def test_player_spawn_is_not_inside_building(self):
        spawn = next(t for t in self.level["things"] if t["type"] == "playerstart")
        x, _, z = spawn["pos"]
        for b in self.level["brushes"]:
            if b.get("nightdrive_solid"):
                bx, _, bz = b["pos"]
                sx, _, sz = b["size"]
                self.assertFalse(vehicle.circle_hits_rect(
                    x, z, vehicle.COLLISION_RADIUS,
                    (bx-sx/2, bz-sz/2, bx+sx/2, bz+sz/2)))


if __name__ == "__main__":
    unittest.main()
