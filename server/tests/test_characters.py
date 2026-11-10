"""Characters made in the dashboard: checking, the prompt they become, and the cues no voice
performs any more."""

import unittest

from brain import characters, config, faces, mouth, personality, thinking


def sample(**over):
    ch = {"name": "Juniper", "voice": "juniper",
          "prompt": "You are Juniper, an old sea captain who calls {human} first mate and distrusts gadgets.",
          "examples": "[happy] Ahoy, {human}!", "wake_words": ["Hey Juniper", "hey junipur"],
          "lines": {"wake": "Aye?", "sleep": "Going below.", "track_on": "Eyes on you, {human}.", "track_off": "At ease."}}
    ch.update(over)
    return ch


class CleanTest(unittest.TestCase):
    def test_a_voice_name_is_kept_as_written(self):
        self.assertEqual(characters.clean(sample())["voice"], "juniper")

    def test_a_missing_voice_is_allowed_and_empty(self):
        """Old files (and a fresh form) have no voice: the .env one is used."""
        ch = sample(voice=None)
        del ch["voice"]
        self.assertEqual(characters.clean(ch)["voice"], "")

    def test_old_fish_keys_are_ignored(self):
        """A character saved before the fork carries voice_id/tts_model: it loads, the keys are
        dropped, and the voice falls back to empty."""
        ch = characters.clean(sample(voice="", voice_id="0123456789abcdef0123456789abcdef",
                                     tts_model="drama-3"))
        self.assertEqual(ch["voice"], "")
        self.assertNotIn("voice_id", ch)
        self.assertNotIn("tts_model", ch)

    def test_hey_name_is_always_a_wake_word(self):
        words = characters.clean(sample(wake_words=["hi juniper"]))["wake_words"]
        self.assertEqual(words[0], "hey juniper")
        self.assertIn("hi juniper", words)

    def test_rejects(self):
        for bad in (dict(name="Rocky"), dict(name="J"), dict(prompt="short"), dict(lines={"wake": "Aye?"}),
                    dict(wake_words=["juniper"]), dict(voice="x" * 121)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                characters.clean(sample(**bad))

    def test_examples_start_with_a_face(self):
        f = lambda s: characters._face_first(s, faces.EMOTIONS)
        self.assertEqual(f("[excited, talking fast] Hah!"), "[excited] [talking fast] Hah!")
        self.assertEqual(f("[sighs] Long day."), "[neutral] [sighs] Long day.")
        self.assertEqual(f("[happy] Yes."), "[happy] Yes.")


class PromptTest(unittest.TestCase):
    def test_character_prompt_has_the_robot_rules(self):
        p = personality._character_prompt(characters.clean(sample()))
        self.assertIn("You are Juniper", p)
        self.assertIn(f"Ahoy, {config.HUMAN_NAME}!", p)
        self.assertNotIn("{human}", p)
        self.assertIn("chosen from: neutral, happy", p)
        self.assertIn("`look`", p)


class CueTest(unittest.TestCase):
    """No voice performs cues: they're dropped from what's spoken and shown."""

    def test_cues_are_dropped(self):
        self.assertEqual(mouth.without_cues("[small laugh] Oh, you again!!"), "Oh, you again!!")
        self.assertEqual(mouth.without_cues("[sighs] Long <slow>day</slow>."), "Long day .")

    def test_face_found_whichever_comes_first(self):
        b = thinking.RobotBrain.__new__(thinking.RobotBrain)
        for text, emotion, rest in (("[happy] [small laugh] Yes.", "happy", "[small laugh] Yes."),
                                    ("[sighs] [sad] Oh no.", "neutral", "[sad] Oh no."),
                                    ("[excited, talking fast] Hah!", "neutral", "[excited, talking fast] Hah!"),
                                    ("[sighs] Long day.", "neutral", "Long day."),
                                    ("[sad] Oh no.", "sad", "Oh no.")):
            b.emotion = "neutral"
            self.assertEqual(b._take_emotion_tag(text, final=False), (rest, True))
            self.assertEqual(b.emotion, emotion)


if __name__ == "__main__":
    unittest.main()
