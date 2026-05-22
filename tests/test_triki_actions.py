import json
import tempfile
import unittest
from pathlib import Path

from triki_actions import (
    ActionBinding,
    ActionExecutor,
    ActionStep,
    TrikiConfig,
    default_action_map,
    default_profile_map,
    load_config,
    parse_macro_text,
    save_config,
)
from triki_key_emitter import KeyEmissionError, NullKeyEmitter, vk_for_key


class FailingEmitter:
    def press_key(self, key_name):
        raise KeyEmissionError(f"cannot emit {key_name}")


class TrikiActionTests(unittest.TestCase):
    def test_default_actions_cover_playable_gestures(self):
        actions = default_action_map()

        self.assertEqual(actions["rotate-cw"].key_name, "right")
        self.assertEqual(actions["rotate-ccw"].key_name, "left")
        self.assertEqual(actions["scrub-cw"].key_name, "page-down")
        self.assertEqual(actions["scrub-ccw"].key_name, "page-up")
        self.assertEqual(actions["lift"].key_name, "enter")
        self.assertEqual(actions["flip-over"].key_name, "space")
        self.assertEqual(actions["back-forth"].key_name, "escape")
        self.assertNotIn("swirl-cw", actions)
        self.assertNotIn("shake", actions)

    def test_default_profiles_cover_common_use_cases(self):
        profiles = default_profile_map()

        self.assertEqual(list(profiles.keys()), ["Default", "WASD Game", "Media", "Presentation", "Which Sausage, Mate?"])
        self.assertEqual(profiles["Default"]["rotate-cw"].key_name, "right")
        self.assertEqual(profiles["WASD Game"]["rotate-cw"].key_name, "d")
        self.assertEqual(profiles["WASD Game"]["rotate-ccw"].key_name, "a")
        self.assertEqual(profiles["WASD Game"]["scrub-cw"].key_name, "w")
        self.assertEqual(profiles["WASD Game"]["scrub-ccw"].key_name, "s")
        self.assertEqual(profiles["WASD Game"]["lift"].key_name, "w")
        self.assertEqual(profiles["Media"]["rotate-cw"].key_name, "volume-up")
        self.assertEqual(profiles["Media"]["scrub-cw"].key_name, "media-next")
        self.assertEqual(profiles["Presentation"]["rotate-ccw"].key_name, "left")
        self.assertEqual(profiles["Presentation"]["scrub-cw"].type, "disabled")
        self.assertEqual(profiles["Which Sausage, Mate?"]["rotate-cw"].key_name, "right")
        self.assertEqual(profiles["Which Sausage, Mate?"]["rotate-ccw"].key_name, "left")
        self.assertEqual(profiles["Which Sausage, Mate?"]["scrub-cw"].key_name, "=")
        self.assertEqual(profiles["Which Sausage, Mate?"]["scrub-ccw"].key_name, "z")
        self.assertEqual(profiles["Which Sausage, Mate?"]["back-forth"].key_name, "backspace")
        self.assertEqual(profiles["Which Sausage, Mate?"]["lift"].key_name, "enter")
        self.assertEqual(profiles["Which Sausage, Mate?"]["flip-over"].key_name, "space")

    def test_empty_config_starts_with_default_profile_set(self):
        config = TrikiConfig().merged_with_defaults()

        self.assertEqual(config.active_profile, "Default")
        self.assertEqual(list(config.profiles.keys()), ["Default", "WASD Game", "Media", "Presentation", "Which Sausage, Mate?"])
        self.assertEqual(config.actions["rotate-cw"].key_name, "right")

    def test_media_keys_are_supported_as_action_targets(self):
        self.assertEqual(vk_for_key("volume-up"), 0xAF)
        self.assertEqual(vk_for_key("volume-down"), 0xAE)
        self.assertEqual(vk_for_key("media-play-pause"), 0xB3)

    def test_action_executor_runs_single_key(self):
        emitter = NullKeyEmitter()
        executor = ActionExecutor(key_emitter=emitter)

        result = executor.execute(ActionBinding.key("volume-up"))

        self.assertTrue(result.emitted)
        self.assertEqual(result.description, "volume-up")
        self.assertEqual(emitter.pressed, ["volume-up"])

    def test_action_executor_runs_macro_with_delay(self):
        emitter = NullKeyEmitter()
        sleeps = []
        executor = ActionExecutor(key_emitter=emitter, sleep=sleeps.append)

        binding = ActionBinding.macro(
            (
                ActionStep.key("left"),
                ActionStep.delay(125),
                ActionStep.key("enter"),
            )
        )
        result = executor.execute(binding)

        self.assertTrue(result.emitted)
        self.assertEqual(emitter.pressed, ["left", "enter"])
        self.assertEqual(sleeps, [0.125])

    def test_parse_macro_text_accepts_key_and_delay_steps(self):
        binding = parse_macro_text("left, 125ms, enter, volume-up")

        self.assertEqual(binding.type, "macro")
        self.assertEqual(binding.steps[0].key_name, "left")
        self.assertEqual(binding.steps[1].delay_ms, 125)
        self.assertEqual(binding.steps[3].key_name, "volume-up")

    def test_disabled_action_does_not_emit(self):
        emitter = NullKeyEmitter()
        executor = ActionExecutor(key_emitter=emitter)

        result = executor.execute(ActionBinding.disabled())

        self.assertFalse(result.emitted)
        self.assertEqual(result.description, "disabled")
        self.assertEqual(emitter.pressed, [])

    def test_key_emission_errors_return_action_result_instead_of_raising(self):
        executor = ActionExecutor(key_emitter=FailingEmitter())

        result = executor.execute(ActionBinding.key("space"))

        self.assertFalse(result.emitted)
        self.assertEqual(result.description, "space")
        self.assertIn("output error", result.reason)
        self.assertIn("cannot emit space", result.reason)

    def test_config_round_trips_to_json(self):
        config = TrikiConfig(
            actions={
                "rotate-cw": ActionBinding.key("right"),
                "back-forth": ActionBinding.macro(
                    (ActionStep.key("escape"), ActionStep.delay(50), ActionStep.key("enter"))
                ),
            },
            output_enabled=True,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            save_config(path, config)
            raw = json.loads(path.read_text(encoding="utf-8"))
            loaded = load_config(path)

        self.assertEqual(raw["version"], 5)
        self.assertTrue(loaded.output_enabled)
        self.assertEqual(loaded.actions["rotate-cw"].key_name, "right")
        self.assertEqual(loaded.actions["back-forth"].steps[2].key_name, "enter")

    def test_load_config_merges_missing_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            path.write_text(
                json.dumps({"version": 3, "actions": {"shake": {"type": "disabled"}}}),
                encoding="utf-8",
            )

            loaded = load_config(path)

        self.assertEqual(loaded.actions["back-forth"].type, "disabled")
        self.assertNotIn("shake", loaded.actions)
        self.assertEqual(loaded.actions["rotate-cw"].key_name, "right")

    def test_config_round_trips_named_profiles_and_active_profile(self):
        config = TrikiConfig(
            profiles={
                "Desktop": {"rotate-cw": ActionBinding.key("right")},
                "Game": {
                    "rotate-cw": ActionBinding.key("d"),
                    "rotate-ccw": ActionBinding.key("a"),
                    "lift": ActionBinding.key("w"),
                },
            },
            active_profile="Game",
            output_enabled=True,
        ).merged_with_defaults()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            save_config(path, config)
            raw = json.loads(path.read_text(encoding="utf-8"))
            loaded = load_config(path)

        self.assertEqual(raw["version"], 5)
        self.assertEqual(raw["active_profile"], "Game")
        self.assertIn("Desktop", raw["profiles"])
        self.assertTrue(loaded.output_enabled)
        self.assertEqual(loaded.active_profile, "Game")
        self.assertEqual(loaded.actions["rotate-cw"].key_name, "d")
        self.assertEqual(loaded.profiles["Desktop"]["rotate-cw"].key_name, "right")

    def test_legacy_config_actions_become_default_profile_and_presets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "actions": {
                            "rotate-cw": {"type": "key", "key": "d"},
                            "rotate-ccw": {"type": "key", "key": "a"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_config(path)

        self.assertEqual(loaded.active_profile, "Default")
        self.assertEqual(loaded.actions["rotate-cw"].key_name, "d")
        self.assertEqual(loaded.profiles["Default"]["rotate-ccw"].key_name, "a")
        self.assertEqual(loaded.profiles["WASD Game"]["rotate-cw"].key_name, "d")
        self.assertEqual(loaded.profiles["WASD Game"]["scrub-cw"].key_name, "w")
        self.assertEqual(loaded.profiles["Media"]["rotate-cw"].key_name, "volume-up")
        self.assertEqual(loaded.profiles["Which Sausage, Mate?"]["scrub-cw"].key_name, "=")

    def test_version_four_builtin_profiles_are_migrated_to_new_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 4,
                        "active_profile": "WASD Game",
                        "profiles": {
                            "Default": {
                                "rotate-cw": {"type": "key", "key": "right"},
                                "rotate-ccw": {"type": "key", "key": "left"},
                                "scrub-cw": {"type": "key", "key": "right"},
                                "scrub-ccw": {"type": "key", "key": "left"},
                                "back-forth": {"type": "key", "key": "escape"},
                                "lift": {"type": "key", "key": "enter"},
                                "flip-over": {"type": "key", "key": "space"},
                            },
                            "WASD Game": {
                                "rotate-cw": {"type": "key", "key": "d"},
                                "rotate-ccw": {"type": "key", "key": "a"},
                                "scrub-cw": {"type": "key", "key": "d"},
                                "scrub-ccw": {"type": "key", "key": "a"},
                                "back-forth": {"type": "key", "key": "escape"},
                                "lift": {"type": "key", "key": "w"},
                                "flip-over": {"type": "key", "key": "space"},
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_config(path)

        self.assertEqual(loaded.version, 5)
        self.assertEqual(loaded.active_profile, "WASD Game")
        self.assertEqual(loaded.actions["scrub-cw"].key_name, "w")
        self.assertEqual(loaded.actions["scrub-ccw"].key_name, "s")
        self.assertEqual(loaded.profiles["Default"]["scrub-cw"].key_name, "page-down")
        self.assertEqual(loaded.profiles["Default"]["scrub-ccw"].key_name, "page-up")
        self.assertIn("Which Sausage, Mate?", loaded.profiles)

    def test_version_four_custom_profile_overrides_survive_builtin_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 4,
                        "active_profile": "Default",
                        "profiles": {
                            "Default": {
                                "rotate-cw": {"type": "key", "key": "right"},
                                "rotate-ccw": {"type": "key", "key": "left"},
                                "scrub-cw": {"type": "key", "key": "x"},
                                "scrub-ccw": {"type": "key", "key": "left"},
                                "back-forth": {"type": "key", "key": "escape"},
                                "lift": {"type": "key", "key": "enter"},
                                "flip-over": {"type": "key", "key": "space"},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_config(path)

        self.assertEqual(loaded.version, 5)
        self.assertEqual(loaded.actions["scrub-cw"].key_name, "x")
        self.assertEqual(loaded.actions["scrub-ccw"].key_name, "page-up")
        self.assertEqual(loaded.profiles["WASD Game"]["scrub-cw"].key_name, "w")
        self.assertIn("Which Sausage, Mate?", loaded.profiles)

    def test_version_two_profile_config_is_migrated_with_presets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "active_profile": "Default",
                        "profiles": {
                            "Default": {
                                "rotate-cw": {"type": "key", "key": "right"},
                                "shake": {"type": "disabled"},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_config(path)

        self.assertEqual(list(loaded.profiles.keys()), ["Default", "WASD Game", "Media", "Presentation", "Which Sausage, Mate?"])
        self.assertEqual(loaded.profiles["Default"]["back-forth"].type, "disabled")
        self.assertNotIn("shake", loaded.profiles["Default"])
        self.assertEqual(loaded.profiles["Media"]["lift"].key_name, "media-play-pause")


if __name__ == "__main__":
    unittest.main()
