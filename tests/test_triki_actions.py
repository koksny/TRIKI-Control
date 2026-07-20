import json
import tempfile
import unittest
from pathlib import Path

from triki_actions import (
    BUILTIN_PROFILE_NAMES,
    CONFIG_VERSION,
    DEFAULT_ENGINE,
    DEFAULT_PROFILE_NAME,
    ENGINE_CLASSIFIER,
    ENGINE_MOTION,
    GAME_PROFILE_NAME,
    MOTION_LABELS,
    MAX_MACRO_DELAY_MS,
    MOTION_PROFILE_NAME,
    MUSIC_PROFILE_NAME,
    ActionBinding,
    ActionExecutor,
    ActionStep,
    MotionProfileSettings,
    TrikiConfig,
    default_actions_for_profile,
    default_action_map,
    default_motion_settings_for_profile,
    default_profile_map,
    engine_for_profile,
    labels_for_profile,
    load_config,
    normalize_engine,
    normalize_hold_ms,
    normalize_mouse_axis_enabled,
    parse_macro_text,
    remap_legacy_profile_name,
    save_config,
)
from triki_key_emitter import (
    DEFAULT_MOUSE_SPEED,
    KeyEmissionError,
    NullKeyEmitter,
    normalize_mouse_speed,
    vk_for_key,
)


class FailingEmitter:
    def press_key(self, key_name):
        raise KeyEmissionError(f"cannot emit {key_name}")


class TrikiActionTests(unittest.TestCase):
    def test_default_actions_cover_playable_gestures(self):
        # default_action_map() is the universal profile fallback: same vocabulary
        # and defaults as Game, so Advanced never shows a stale per-profile table.
        actions = default_action_map()

        self.assertEqual(actions, default_profile_map()["Game"])
        self.assertEqual(set(actions), set(MOTION_LABELS))
        self.assertNotIn("swirl-cw", actions)
        self.assertNotIn("shake", actions)

    def test_exactly_two_builtin_profiles_game_and_music(self):
        profiles = default_profile_map()

        # EXACTLY two built-ins, in order: Game (default) then Music.
        self.assertEqual(list(profiles.keys()), ["Game", "Music"])
        self.assertEqual(set(profiles.keys()), set(BUILTIN_PROFILE_NAMES))
        self.assertEqual(DEFAULT_PROFILE_NAME, "Game")
        # No legacy profile survives.
        for gone in (
            "Default",
            "WASD Game",
            "Presentation",
            "Which Sausage, Mate?",
            "Doom",
            "Doom Motion",
            "Doom / Steering",
            "Experimental Pointer",
            "Media",
        ):
            self.assertNotIn(gone, profiles)

    def test_game_profile_uses_doom_default_bound_keys(self):
        game = default_profile_map()["Game"]

        # The Game profile is keyed by the current first-class motion controls.
        self.assertEqual(game["turn-left"].key_name, "left")
        self.assertEqual(game["turn-right"].key_name, "right")
        self.assertEqual(game["go"].key_name, "w")
        self.assertEqual(game["stamp"].key_name, "enter")
        self.assertEqual(game["flip"].key_name, "shift")
        self.assertEqual(game["scrub-straight"].key_name, "space")
        # The dead discrete overload is gone: no legacy classifier rows in Game.
        self.assertNotIn("scrub-cw", game)
        self.assertNotIn("flip-over", game)

    def test_every_profile_uses_motion_rows_but_profile_defaults(self):
        profiles = default_profile_map()
        game = profiles["Game"]
        music = profiles["Music"]

        self.assertEqual(default_actions_for_profile("Arena"), game)
        self.assertEqual(labels_for_profile("Game"), MOTION_LABELS)
        self.assertEqual(labels_for_profile("Music"), MOTION_LABELS)
        self.assertEqual(labels_for_profile("Arena"), MOTION_LABELS)
        self.assertEqual(set(music), set(MOTION_LABELS))
        self.assertEqual(music["turn-left"].key_name, "volume-down")
        self.assertEqual(music["turn-right"].key_name, "volume-up")
        self.assertEqual(music["go"].key_name, "media-prev")
        self.assertEqual(music["stamp"].key_name, "media-play-pause")
        self.assertEqual(music["flip"].key_name, "volume-mute")
        self.assertEqual(music["scrub-straight"].key_name, "media-next")

    def test_builtin_profiles_have_separate_motion_tuning_defaults(self):
        game = default_motion_settings_for_profile("Game")
        music = default_motion_settings_for_profile("Music")

        self.assertEqual(game.turn_threshold, 1000.0)
        self.assertLess(music.turn_threshold, game.turn_threshold)
        self.assertEqual(game.turn_sensitivity, 50.0)
        self.assertGreater(music.turn_sensitivity, game.turn_sensitivity)
        self.assertEqual(game.mouse_speed, DEFAULT_MOUSE_SPEED)
        self.assertEqual(music.mouse_speed, DEFAULT_MOUSE_SPEED)
        self.assertTrue(game.mouse_axis_enabled)
        self.assertTrue(music.mouse_axis_enabled)

    def test_empty_config_starts_with_two_profiles_active_game(self):
        config = TrikiConfig().merged_with_defaults()

        self.assertEqual(config.active_profile, "Game")
        self.assertEqual(list(config.profiles.keys()), ["Game", "Music"])
        self.assertEqual(config.engine, ENGINE_MOTION)
        self.assertEqual(config.actions["turn-right"].key_name, "right")
        self.assertIn("Game", config.profile_settings)
        self.assertIn("Music", config.profile_settings)

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

    def test_action_executor_runs_mouse_button_and_move_actions(self):
        emitter = NullKeyEmitter()
        executor = ActionExecutor(key_emitter=emitter)

        button_result = executor.execute(ActionBinding.key("mouse-left-button"))
        move_result = executor.execute(ActionBinding.key("mouse-move-right"))

        self.assertTrue(button_result.emitted)
        self.assertTrue(move_result.emitted)
        self.assertEqual(emitter.pressed, ["mouse-left-button"])
        self.assertEqual(emitter.pointer_moves, [(DEFAULT_MOUSE_SPEED, 0)])

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

        self.assertEqual(raw["version"], CONFIG_VERSION)
        self.assertTrue(loaded.output_enabled)
        # The collapse discards the stored top-level actions (old discrete labels)
        # and ships the fresh first-class Game built-in (active profile -> Game).
        self.assertEqual(loaded.actions["turn-right"].key_name, "right")
        self.assertEqual(loaded.active_profile, "Game")

    def test_mouse_settings_round_trip_and_invalid_values_use_profile_fallback(self):
        config = TrikiConfig(
            version=CONFIG_VERSION,
            profile_settings={
                "Game": MotionProfileSettings(mouse_speed=24, mouse_axis_enabled=False),
                "Music": {"mouse_speed": "bad", "mouse_axis_enabled": "bad"},
            },
        ).merged_with_defaults()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            save_config(path, config)
            loaded = load_config(path)

        self.assertEqual(loaded.profile_settings["Game"].mouse_speed, 24)
        self.assertFalse(loaded.profile_settings["Game"].mouse_axis_enabled)
        self.assertEqual(loaded.profile_settings["Music"].mouse_speed, DEFAULT_MOUSE_SPEED)
        self.assertTrue(loaded.profile_settings["Music"].mouse_axis_enabled)
        self.assertEqual(normalize_mouse_speed(-100), 1)
        self.assertEqual(normalize_mouse_speed(999), 50)
        self.assertFalse(normalize_mouse_axis_enabled("off"))
        self.assertTrue(normalize_mouse_axis_enabled("not-a-mode"))

    def test_config_save_is_atomic_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"

            save_config(path, TrikiConfig().merged_with_defaults())

            self.assertTrue(path.exists())
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_load_config_collapses_to_two_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            path.write_text(
                json.dumps({"version": 3, "actions": {"shake": {"type": "disabled"}}}),
                encoding="utf-8",
            )

            loaded = load_config(path)

        self.assertEqual(list(loaded.profiles.keys()), ["Game", "Music"])
        self.assertNotIn("shake", loaded.actions)
        self.assertEqual(loaded.actions["turn-right"].key_name, "right")

    def test_config_keeps_custom_profiles_alongside_two_builtins(self):
        # Custom (user-created) profiles survive the collapse; the two built-ins are
        # always added. A custom active profile stays active.
        config = TrikiConfig(
            profiles={
                "Desktop": {"turn-right": ActionBinding.key("right")},
                "My Doom": {
                    "turn-right": ActionBinding.key("d"),
                    "stamp": ActionBinding.key("enter"),
                },
            },
            active_profile="My Doom",
            output_enabled=True,
        ).merged_with_defaults()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            save_config(path, config)
            raw = json.loads(path.read_text(encoding="utf-8"))
            loaded = load_config(path)

        self.assertEqual(raw["version"], CONFIG_VERSION)
        self.assertEqual(sorted(raw["profiles"].keys()), ["Desktop", "Game", "Music", "My Doom"])
        # The two built-ins always exist.
        self.assertIn("Game", loaded.profiles)
        self.assertIn("Music", loaded.profiles)
        self.assertTrue(loaded.output_enabled)
        self.assertEqual(loaded.active_profile, "My Doom")
        self.assertEqual(loaded.actions["turn-right"].key_name, "d")
        self.assertEqual(set(loaded.actions), set(MOTION_LABELS))

    def test_legacy_v7_nine_profile_config_collapses_to_two(self):
        # The real on-disk world: a v7 config with the nine historical profiles and
        # active='WASD Game'. It must collapse to EXACTLY {Game, Music}, remap the
        # active profile to Game, derive engine='motion', and bump version to 9.
        nine = {
            name: {"rotate-cw": {"type": "key", "key": "right"}}
            for name in (
                "Default",
                "WASD Game",
                "Media",
                "Presentation",
                "Which Sausage, Mate?",
                "Doom",
                "Doom Motion",
                "Doom / Steering",
                "Experimental Pointer",
            )
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 7,
                        "active_profile": "WASD Game",
                        "engine": "classifier",
                        "profiles": nine,
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_config(path)

        self.assertEqual(list(loaded.profiles.keys()), ["Game", "Music"])
        self.assertEqual(loaded.active_profile, "Game")
        self.assertEqual(loaded.engine, ENGINE_MOTION)
        self.assertEqual(loaded.version, CONFIG_VERSION)
        # The deleted profiles do NOT survive.
        self.assertNotIn("Experimental Pointer", loaded.profiles)
        self.assertNotIn("Doom / Steering", loaded.profiles)
        self.assertNotIn("Doom Motion", loaded.profiles)
        # Game ships its fresh first-class defaults (not the stored 'right' on every
        # row); the old discrete labels never leak into the motion vocabulary.
        self.assertEqual(loaded.profiles["Game"], default_profile_map()["Game"])
        self.assertEqual(loaded.profiles["Music"], default_profile_map()["Music"])

    def test_legacy_active_media_remaps_to_music(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 7,
                        "active_profile": "Media",
                        "profiles": {"Media": {"lift": {"type": "key", "key": "media-play-pause"}}},
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_config(path)

        self.assertEqual(loaded.active_profile, "Music")
        self.assertEqual(loaded.engine, ENGINE_MOTION)
        self.assertEqual(list(loaded.profiles.keys()), ["Game", "Music"])
        self.assertEqual(loaded.actions, default_profile_map()["Music"])

    def test_v14_music_game_defaults_reset_to_media_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 14,
                        "active_profile": "Music",
                        "profiles": {"Music": {
                            "turn-left": {"type": "key", "key": "left"},
                            "turn-right": {"type": "key", "key": "right"},
                            "go": {"type": "key", "key": "w"},
                            "stamp": {"type": "key", "key": "enter"},
                            "flip": {"type": "key", "key": "shift"},
                            "scrub-straight": {"type": "key", "key": "space"},
                        }},
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_config(path)

        self.assertEqual(loaded.version, CONFIG_VERSION)
        self.assertEqual(loaded.active_profile, "Music")
        self.assertEqual(loaded.profiles["Music"], default_profile_map()["Music"])
        self.assertEqual(loaded.actions, default_profile_map()["Music"])

    def test_current_music_custom_bindings_survive_config_merge(self):
        config = TrikiConfig(
            version=CONFIG_VERSION,
            active_profile="Music",
            profiles={"Music": {"stamp": ActionBinding.key("space")}},
        ).merged_with_defaults()

        self.assertEqual(config.profiles["Music"]["stamp"].key_name, "space")
        self.assertEqual(config.profiles["Music"]["turn-right"].key_name, "volume-up")

    def test_v1_0_1_and_v1_0_2_game_bindings_survive_config_merge(self):
        # v1.0.1 (schema 14) and v1.0.2 (schema 15) already used the current
        # Game/Doom motion rows. Later motion-tuning schema bumps must not erase
        # a user's Doom key overrides.
        for version in (14, 15):
            with self.subTest(version=version):
                config = TrikiConfig(
                    version=version,
                    active_profile="Game",
                    profiles={"Game": {"stamp": ActionBinding.key("ctrl")}},
                    actions={"turn-right": ActionBinding.key("d")},
                ).merged_with_defaults()

                self.assertEqual(config.profiles["Game"]["stamp"].key_name, "ctrl")
                self.assertEqual(config.actions["turn-right"].key_name, "d")
                self.assertEqual(config.actions["stamp"].key_name, "ctrl")

    def test_v1_0_2_music_bindings_survive_config_merge(self):
        # v1.0.2 (schema 15) fixed Music to use media defaults, so user edits
        # from that version are already safe to preserve.
        config = TrikiConfig(
            version=15,
            active_profile="Music",
            profiles={"Music": {"stamp": ActionBinding.key("space")}},
            actions={"turn-left": ActionBinding.key("a")},
        ).merged_with_defaults()

        self.assertEqual(config.profiles["Music"]["stamp"].key_name, "space")
        self.assertEqual(config.actions["turn-left"].key_name, "a")
        self.assertEqual(config.actions["stamp"].key_name, "space")

    def test_profile_motion_settings_round_trip_with_config(self):
        config = TrikiConfig(
            active_profile="Music",
            profile_settings={
                "Game": MotionProfileSettings(turn_threshold=1000.0, turn_sensitivity=50.0),
                "Music": MotionProfileSettings(turn_threshold=580.0, turn_sensitivity=92.0),
            },
        ).merged_with_defaults()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            save_config(path, config)
            raw = json.loads(path.read_text(encoding="utf-8"))
            loaded = load_config(path)

        self.assertEqual(raw["profile_settings"]["Music"]["turn_threshold"], 580.0)
        self.assertNotIn("tilt_threshold", raw["profile_settings"]["Music"])
        self.assertEqual(raw["profile_settings"]["Music"]["turn_sensitivity"], 92.0)
        self.assertEqual(loaded.profile_settings["Game"].turn_sensitivity, 50.0)
        self.assertEqual(loaded.profile_settings["Music"].turn_threshold, 580.0)

    def test_profile_motion_settings_are_clamped(self):
        settings = MotionProfileSettings.from_dict(
            {"turn_threshold": 9999, "turn_sensitivity": -20}
        )

        self.assertEqual(settings.turn_threshold, 1600.0)
        self.assertEqual(settings.turn_sensitivity, 0.0)

    def test_partial_music_motion_settings_keep_music_fallbacks(self):
        config = TrikiConfig(
            version=CONFIG_VERSION,
            profile_settings={"Music": {"turn_threshold": 620.0}},
        ).merged_with_defaults()

        self.assertEqual(config.profile_settings["Music"].turn_threshold, 620.0)
        self.assertEqual(
            config.profile_settings["Music"].turn_sensitivity,
            default_motion_settings_for_profile("Music").turn_sensitivity,
        )

    def test_invalid_music_motion_settings_keep_music_fallbacks(self):
        config = TrikiConfig(
            version=CONFIG_VERSION,
            profile_settings={"Music": {"turn_threshold": "bad", "turn_sensitivity": "bad"}},
        ).merged_with_defaults()

        self.assertEqual(config.profile_settings["Music"], default_motion_settings_for_profile("Music"))

    def test_legacy_config_actions_only_collapses_to_game(self):
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

        self.assertEqual(loaded.active_profile, "Game")
        self.assertEqual(list(loaded.profiles.keys()), ["Game", "Music"])
        # Fresh first-class Game defaults, not the stored legacy actions.
        self.assertEqual(loaded.actions["turn-right"].key_name, "right")


class RemapLegacyProfileNameTests(unittest.TestCase):
    def test_builtins_pass_through(self):
        self.assertEqual(remap_legacy_profile_name("Game"), "Game")
        self.assertEqual(remap_legacy_profile_name("Music"), "Music")

    def test_media_maps_to_music(self):
        self.assertEqual(remap_legacy_profile_name("Media"), "Music")
        self.assertEqual(remap_legacy_profile_name("media"), "Music")

    def test_every_other_legacy_name_maps_to_game(self):
        for legacy in (
            "Default",
            "WASD Game",
            "Presentation",
            "Which Sausage, Mate?",
            "Doom",
            "Doom Motion",
            "Doom / Steering",
            "Experimental Pointer",
            "totally unknown",
        ):
            self.assertEqual(remap_legacy_profile_name(legacy), "Game")


class HoldMsConfigTests(unittest.TestCase):
    def test_normalize_hold_ms_clamps_and_defaults(self):
        self.assertEqual(normalize_hold_ms(0), 0)
        self.assertEqual(normalize_hold_ms(400), 400)
        self.assertEqual(normalize_hold_ms(-50), 0)
        self.assertEqual(normalize_hold_ms(999999), 2000)
        self.assertEqual(normalize_hold_ms("nonsense"), 0)

    def test_hold_ms_round_trips_through_config(self):
        config = TrikiConfig(hold_ms=350).merged_with_defaults()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            save_config(path, config)
            raw = json.loads(path.read_text(encoding="utf-8"))
            loaded = load_config(path)

        self.assertEqual(raw["hold_ms"], 350)
        self.assertEqual(loaded.hold_ms, 350)

    def test_missing_hold_ms_defaults_to_zero(self):
        loaded = TrikiConfig.from_dict({"version": 6})
        self.assertEqual(loaded.hold_ms, 0)

    def test_old_config_drops_legacy_builtins_but_keeps_custom_profiles(self):
        # Legacy BUILT-IN names (Default) are dropped; a user-created custom profile
        # (My Game) survives. The two built-ins are always present.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 5,
                        "active_profile": "Default",
                        "profiles": {
                            "Default": {"rotate-cw": {"type": "key", "key": "right"}},
                            "My Game": {"scrub-cw": {"type": "key", "key": "x"}},
                        },
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_config(path)

        self.assertEqual(loaded.version, CONFIG_VERSION)
        self.assertNotIn("Default", loaded.profiles)  # legacy built-in dropped
        self.assertIn("Game", loaded.profiles)
        self.assertIn("Music", loaded.profiles)
        self.assertIn("My Game", loaded.profiles)  # custom profile kept
        self.assertEqual(loaded.profiles["My Game"], default_profile_map()["Game"])
        # active 'Default' was a legacy built-in -> remaps to Game.
        self.assertEqual(loaded.active_profile, "Game")


class LoadConfigResilienceTests(unittest.TestCase):
    def _assert_falls_back_with_backup(self, raw_text: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            path.write_text(raw_text, encoding="utf-8")
            backup = path.with_suffix(path.suffix + ".bak")

            loaded = load_config(path)

            self.assertTrue(backup.exists(), f"expected backup for: {raw_text!r}")
            self.assertEqual(backup.read_text(encoding="utf-8"), raw_text)
        self.assertEqual(loaded.active_profile, "Game")
        self.assertEqual(loaded.actions["turn-right"].key_name, "right")

    def test_malformed_json_falls_back_to_defaults_and_backs_up(self):
        self._assert_falls_back_with_backup("{not valid json")

    def test_list_top_level_falls_back_to_defaults_and_backs_up(self):
        self._assert_falls_back_with_backup(json.dumps([1, 2, 3]))

    def test_null_top_level_falls_back_to_defaults_and_backs_up(self):
        self._assert_falls_back_with_backup(json.dumps(None))

    def test_garbage_version_falls_back_to_defaults_and_backs_up(self):
        self._assert_falls_back_with_backup(
            json.dumps({"version": "garbage", "active_profile": "Default"})
        )

    def test_negative_macro_delay_falls_back_to_defaults_and_backs_up(self):
        self._assert_falls_back_with_backup(
            json.dumps(
                {
                    "version": 6,
                    "active_profile": "Default",
                    "profiles": {
                        "Default": {
                            "back-forth": {
                                "type": "macro",
                                "steps": [{"type": "delay", "ms": -5}],
                            }
                        }
                    },
                }
            )
        )

    def test_valid_legacy_config_loads_without_backup(self):
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
            backup = path.with_suffix(path.suffix + ".bak")

            loaded = load_config(path)

            self.assertFalse(backup.exists())
        # A valid (parseable) legacy config still loads without a backup, but the
        # collapse ships the fresh Game built-in (old custom rows are discarded).
        self.assertEqual(list(loaded.profiles.keys()), ["Game", "Music"])
        self.assertEqual(loaded.actions["turn-right"].key_name, "right")

    def test_from_dict_rejects_non_object_payload(self):
        with self.assertRaises(ValueError):
            TrikiConfig.from_dict([1, 2, 3])


class MacroValidationTests(unittest.TestCase):
    def test_empty_macro_text_reports_helpful_message(self):
        for text in ("", "  ,  , "):
            with self.assertRaises(ValueError) as context:
                parse_macro_text(text)
            self.assertIn("enter at least one key or delay", str(context.exception))

    def test_bare_ms_delay_reports_invalid_macro_delay(self):
        with self.assertRaises(ValueError) as context:
            parse_macro_text("ms")
        self.assertIn("invalid macro delay", str(context.exception))

    def test_macro_delay_is_clamped_to_ceiling(self):
        self.assertEqual(ActionStep.delay(999999999).delay_ms, MAX_MACRO_DELAY_MS)
        self.assertEqual(
            parse_macro_text("left, 999999999ms").steps[1].delay_ms, MAX_MACRO_DELAY_MS
        )


class ForwardCompatibleMigrationTests(unittest.TestCase):
    def test_version_above_current_collapses_to_two_and_clamps_version(self):
        # A future / higher-version config also collapses to the two built-ins and
        # the version is clamped to CONFIG_VERSION.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 99,
                        "active_profile": "Default",
                        "profiles": {"Default": {"rotate-cw": {"type": "key", "key": "x"}}},
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_config(path)
            save_config(path, loaded)
            raw = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(sorted(loaded.profiles.keys()), ["Game", "Music"])
        self.assertEqual(loaded.active_profile, "Game")
        self.assertEqual(loaded.version, CONFIG_VERSION)
        self.assertEqual(raw["version"], CONFIG_VERSION)


class EngineSelectionTests(unittest.TestCase):
    def test_every_profile_uses_motion_engine(self):
        self.assertEqual(engine_for_profile(GAME_PROFILE_NAME), ENGINE_MOTION)
        self.assertEqual(engine_for_profile(MUSIC_PROFILE_NAME), ENGINE_MOTION)
        # MOTION_PROFILE_NAME is an alias of Game.
        self.assertEqual(MOTION_PROFILE_NAME, GAME_PROFILE_NAME)
        self.assertEqual(engine_for_profile(MOTION_PROFILE_NAME), ENGINE_MOTION)
        # Custom/unknown names use the same motion engine and settings as Game.
        self.assertEqual(engine_for_profile("Doom"), ENGINE_MOTION)
        self.assertEqual(engine_for_profile("whatever"), ENGINE_MOTION)

    def test_default_config_uses_motion_engine(self):
        # The default profile is Game, so the default engine is motion.
        config = TrikiConfig().merged_with_defaults()
        self.assertEqual(config.active_profile, GAME_PROFILE_NAME)
        self.assertEqual(config.engine, ENGINE_MOTION)
        self.assertEqual(DEFAULT_ENGINE, ENGINE_MOTION)

    def test_selecting_music_sets_motion_engine(self):
        config = TrikiConfig(active_profile=MUSIC_PROFILE_NAME).merged_with_defaults()
        self.assertEqual(config.active_profile, MUSIC_PROFILE_NAME)
        self.assertEqual(config.engine, ENGINE_MOTION)

    def test_normalize_engine_defaults_and_validates(self):
        # normalize_engine still validates raw values; missing/unknown -> motion
        # (the new DEFAULT_ENGINE), known values pass through.
        self.assertEqual(normalize_engine(None), ENGINE_MOTION)
        self.assertEqual(normalize_engine(""), ENGINE_MOTION)
        self.assertEqual(normalize_engine("nonsense"), ENGINE_MOTION)
        self.assertEqual(normalize_engine("classifier"), ENGINE_CLASSIFIER)
        self.assertEqual(normalize_engine("motion"), ENGINE_MOTION)
        self.assertEqual(normalize_engine("  MOTION  "), ENGINE_MOTION)

    def test_engine_derives_from_active_profile_not_stale_stored_value(self):
        # A stored engine that disagrees with the active profile is reconciled to
        # the profile, so the two never drift apart. (Active 'Default' collapses to
        # Game -> motion regardless of a stored 'classifier'.)
        loaded = TrikiConfig.from_dict(
            {"version": 7, "engine": "classifier", "active_profile": "Default"}
        )
        self.assertEqual(loaded.active_profile, GAME_PROFILE_NAME)
        self.assertEqual(loaded.engine, ENGINE_MOTION)
        loaded2 = TrikiConfig.from_dict(
            {"version": 7, "engine": "motion", "active_profile": "Media"}
        )
        self.assertEqual(loaded2.active_profile, MUSIC_PROFILE_NAME)
        self.assertEqual(loaded2.engine, ENGINE_MOTION)

    def test_engine_round_trips_through_config(self):
        config = TrikiConfig(active_profile=GAME_PROFILE_NAME).merged_with_defaults()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triki.json"
            save_config(path, config)
            raw = json.loads(path.read_text(encoding="utf-8"))
            loaded = load_config(path)

        self.assertEqual(raw["engine"], ENGINE_MOTION)
        self.assertEqual(loaded.engine, ENGINE_MOTION)
        self.assertEqual(loaded.active_profile, GAME_PROFILE_NAME)
        # A Music config persists engine == "motion" because every profile now
        # shares the Game control engine/settings.
        music_config = TrikiConfig(active_profile=MUSIC_PROFILE_NAME).merged_with_defaults()
        self.assertEqual(music_config.to_dict()["engine"], ENGINE_MOTION)


class AutoHoldTests(unittest.TestCase):
    def test_game_profile_keeps_continuous_hold_when_set(self):
        # The Game profile (motion) auto-holds: a non-zero hold_ms is retained so
        # the continuous re-emitted intent keeps the key held (no toggle needed).
        config = TrikiConfig(active_profile=GAME_PROFILE_NAME, hold_ms=200).merged_with_defaults()
        self.assertEqual(config.engine, ENGINE_MOTION)
        self.assertEqual(config.hold_ms, 200)

    def test_music_profile_keeps_continuous_hold_when_set(self):
        # Music uses the same Motion/Game settings, so hold behaves like Game.
        config = TrikiConfig(active_profile=MUSIC_PROFILE_NAME, hold_ms=350).merged_with_defaults()
        self.assertEqual(config.engine, ENGINE_MOTION)
        self.assertEqual(config.hold_ms, 350)


if __name__ == "__main__":
    unittest.main()
