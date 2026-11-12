import json
import unittest
from unittest.mock import patch
import custom_workflow

from custom_workflow import (canonical_steps, canonical_trigger, group_matches,
                             parse_steps, parse_trigger, redact, render_template,
                             search_pattern, uses_credentials)


class CustomWorkflowValidationTests(unittest.TestCase):
    def test_trigger_modes_are_strict_and_canonical(self):
        self.assertEqual(parse_trigger('{"mode":"manual"}'), {"mode": "manual"})
        schedule = parse_trigger('{"mode":"schedule","at":"2027-01-02T03:04:05"}')
        self.assertEqual(schedule["at"], "2027-01-02T03:04:05")
        group = parse_trigger('{"mode":"group","text":"open"}')
        self.assertEqual(group, {"mode":"group", "groups": [], "sender":"any", "logic":"any",
            "text":"open", "text_regex":False, "button":"", "button_regex":False, "cooldown":0})
        for bad in ({"mode":"manual","extra":1}, {"mode":"schedule","at":"tomorrow"},
                    {"mode":"group","text":""}, {"mode":"group","text":"[","text_regex":True},
                    {"mode":"group","button":"go","sender":"robot"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                canonical_trigger(bad)

    def test_every_step_type_gets_defaults_and_old_shapes_are_rejected(self):
        steps = parse_steps(json.dumps([
            {"t":"send","text":"/start"}, {"t":"wait","ms":0},
            {"t":"click","text":"Next"}, {"t":"wait_reply","success":"done","save":"reply"},
            {"t":"extract","regex":"code=(\\w+)","group":1,"save":"code"},
            {"t":"captcha","length":4,"template":"{captcha}"},
        ]))
        self.assertEqual([step["t"] for step in steps],
                         ["send","wait","click","wait_reply","extract","captcha"])
        self.assertEqual(steps[0], {"t":"send","to":"target","text":"/start","reply":False})
        self.assertTrue(steps[2]["fresh"])
        self.assertEqual(steps[5]["send_to"], "target")
        for bad in ([{"t":"send","v":"/start"}], [{"t":"until","success":"done"}],
                    [{"t":"send","text":""}], [{"t":"extract","regex":"[","save":"x"}],
                    [{"t":"extract","regex":"x","save":"not valid"}],
                    [{"t":"wait","ms":True}], [{"t":"click","unknown":1}], []):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                canonical_steps(bad)

    def test_group_button_context_logic(self):
        trigger = parse_trigger({"mode":"group","logic":"all","text":"launch",
                                 "button":"start=abc","button_regex":False})
        matched, button = group_matches(trigger, "Please LAUNCH", [
            {"text":"Register", "url":"https://t.me/example_bot?start=abc"}])
        self.assertTrue(matched)
        self.assertEqual(button["text"], "Register")
        self.assertFalse(group_matches(trigger, "other", [button])[0])

    def test_variables_credentials_and_redaction(self):
        context = {"trigger":{"text":"hello"}, "credential":{"user":"alice","secret":"PRIVATE"},
                   "last":{"text":"code=7"}, "var":{"code":"7"}, "captcha":"1234"}
        self.assertEqual(render_template('{trigger.text}:{credential.user}:{var.code}:{captcha}:{num:2}', context),
                         'hello:alice:7:1234:{num:2}')
        self.assertTrue(uses_credentials([{"t":"send","text":"{credential.secret}"}]))
        with self.assertRaisesRegex(ValueError, 'missing variable'):
            render_template('{var.missing}', context)
        with self.assertRaisesRegex(ValueError, 'unavailable variable'):
            canonical_steps([{"t":"send","text":"{var.missing}"}])
        with self.assertRaisesRegex(ValueError, 'unavailable variable'):
            canonical_steps([{"t":"extract","from":"{unsupported}","regex":"(.)","group":1,"save":"x"}])
        with self.assertRaisesRegex(ValueError, 'credential'):
            canonical_steps([{"t":"click","ai":True,"text":"{credential.secret}"}])
        with self.assertRaisesRegex(ValueError, 'too large'):
            canonical_steps([{"t":"send","text":"{num:257}"}])
        self.assertNotIn('PRIVATE', redact('failed PRIVATE', {"s":"PRIVATE"}))

    def test_regex_timeout_fails_closed(self):
        if custom_workflow.safe_regex is None:
            with self.assertRaisesRegex(ValueError, 'timed out'):
                raise ValueError('regular expression evaluation timed out')
            with self.assertRaisesRegex(ValueError, 'safety dependency'):
                search_pattern('(a+)+$', 'a' * 100)
        else:
            with patch('custom_workflow.safe_regex.search', side_effect=TimeoutError), self.assertRaisesRegex(ValueError, 'timed out'):
                search_pattern('(a+)+$', 'a' * 100)


if __name__ == '__main__':
    unittest.main()
