"""Strict custom-task configuration shared by API, import, AI, and runtime."""
from datetime import datetime
import json
import re
import regex as safe_regex


MAX_TEXT = 4096
MAX_DEST = 512
MAX_PATTERN = 1024
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_TEMPLATE = re.compile(r"\{([^{}]+)\}")
_VARIABLES = {
    "trigger.text", "trigger.chat_id", "trigger.message_id", "trigger.sender",
    "trigger.button_text", "trigger.button_url", "trigger.bot", "trigger.payload",
    "credential.user", "credential.secret", "last.text", "captcha", "code",
}


def _json(value, label):
    if isinstance(value, (dict, list)):
        return value

    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"{label} contains a duplicate key")
            result[key] = item
        return result

    if not isinstance(value, str):
        raise ValueError(f"{label} must be JSON")
    try:
        return json.loads(value, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (TypeError, ValueError, RecursionError):
        raise ValueError(f"{label} must be valid JSON") from None


def _object(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"{label} has unknown or invalid fields")
    return value


def _string(value, label, maximum=MAX_TEXT, empty=True):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f"{label} must be a string of at most {maximum} characters")
    return value.strip() if not empty else value


def _integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer from {low} to {high}")
    return value


def _boolean(value, label):
    if type(value) is not bool:
        raise ValueError(f"{label} must be boolean")
    return value


def _pattern(value, label, required=False):
    value = _string(value, label, MAX_PATTERN, empty=not required)
    if value:
        try:
            safe_regex.compile(value)
        except safe_regex.error:
            raise ValueError(f"{label} is not a valid regular expression") from None
    return value


def _identifier(value, label, empty=True):
    value = _string(value, label, 64, empty=empty)
    if value and not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be an identifier")
    return value


def _destination(value, label):
    return _string(value, label, MAX_DEST, empty=False)


def canonical_trigger(value):
    trigger = _json(value or '{"mode":"manual"}', "custom_trigger")
    if not isinstance(trigger, dict) or trigger.get("mode") not in ("manual", "schedule", "group"):
        raise ValueError("custom_trigger mode must be manual, schedule, or group")
    mode = trigger["mode"]
    if mode == "manual":
        _object(trigger, {"mode"}, "manual trigger")
        result = {"mode": "manual"}
    elif mode == "schedule":
        _object(trigger, {"mode", "at"}, "schedule trigger")
        at = _string(trigger.get("at"), "schedule at", 19, empty=False)
        try:
            datetime.strptime(at, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            raise ValueError("schedule at must use YYYY-MM-DDTHH:MM:SS") from None
        result = {"mode": "schedule", "at": at}
    else:
        fields = {"mode", "groups", "sender", "logic", "text", "text_regex",
                  "button", "button_regex", "cooldown"}
        _object(trigger, fields, "group trigger")
        groups = trigger.get("groups", [])
        if not isinstance(groups, list) or len(groups) > 50:
            raise ValueError("group trigger groups must be an array with at most 50 entries")
        groups = [_string(group, "group", 256, empty=False) for group in groups]
        if len(set(groups)) != len(groups):
            raise ValueError("group trigger groups must not contain duplicates")
        sender = trigger.get("sender", "any")
        logic = trigger.get("logic", "any")
        if sender not in ("any", "bot", "user") or logic not in ("any", "all"):
            raise ValueError("group trigger sender or logic is invalid")
        text_regex = _boolean(trigger.get("text_regex", False), "text_regex")
        button_regex = _boolean(trigger.get("button_regex", False), "button_regex")
        text = _string(trigger.get("text", ""), "trigger text", MAX_PATTERN)
        button = _string(trigger.get("button", ""), "trigger button", MAX_PATTERN)
        if not text and not button:
            raise ValueError("group trigger requires text or button")
        if text_regex:
            _pattern(text, "trigger text", required=True)
        if button_regex:
            _pattern(button, "trigger button", required=True)
        result = {"mode": "group", "groups": groups, "sender": sender, "logic": logic,
                  "text": text, "text_regex": text_regex, "button": button,
                  "button_regex": button_regex,
                  "cooldown": _integer(trigger.get("cooldown", 0), "cooldown", 0, 86400)}
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def canonical_steps(value):
    steps = _json(value, "custom_steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 50:
        raise ValueError("custom_steps must contain 1 to 50 steps")
    result = []
    schemas = {
        "send": {"t", "to", "text", "reply"},
        "wait": {"t", "ms"},
        "click": {"t", "to", "text", "emoji", "index", "any", "ai", "fresh", "timeout_ms"},
        "wait_reply": {"t", "from", "success", "fail", "regex", "timeout_ms", "save"},
        "extract": {"t", "from", "regex", "group", "save"},
        "captcha": {"t", "from", "length", "send_to", "template"},
    }
    for index, raw in enumerate(steps):
        if not isinstance(raw, dict) or raw.get("t") not in schemas:
            raise ValueError(f"custom_steps[{index}] has an invalid type")
        kind = raw["t"]
        _object(raw, schemas[kind], f"custom_steps[{index}]")
        if kind == "send":
            step = {"t": kind, "to": _destination(raw.get("to", "target"), "send to"),
                    "text": _string(raw.get("text"), "send text", MAX_TEXT, empty=False),
                    "reply": _boolean(raw.get("reply", False), "send reply")}
        elif kind == "wait":
            step = {"t": kind, "ms": _integer(raw.get("ms", 1000), "wait ms", 0, 3600000)}
        elif kind == "click":
            step = {"t": kind, "to": _destination(raw.get("to", "target"), "click to"),
                    "text": _string(raw.get("text", ""), "click text", 1024),
                    "emoji": _string(raw.get("emoji", ""), "click emoji", 128),
                    "index": _integer(raw.get("index", 0), "click index", 0, 100),
                    "any": _boolean(raw.get("any", False), "click any"),
                    "ai": _boolean(raw.get("ai", False), "click ai"),
                    "fresh": _boolean(raw.get("fresh", True), "click fresh"),
                    "timeout_ms": _integer(raw.get("timeout_ms", 30000), "click timeout_ms", 1, 3600000)}
        elif kind == "wait_reply":
            regex = _boolean(raw.get("regex", False), "wait_reply regex")
            success = _string(raw.get("success", ""), "wait_reply success", MAX_PATTERN)
            fail = _string(raw.get("fail", ""), "wait_reply fail", MAX_PATTERN)
            if regex:
                _pattern(success, "wait_reply success")
                _pattern(fail, "wait_reply fail")
            step = {"t": kind, "from": _destination(raw.get("from", "target"), "wait_reply from"),
                    "success": success, "fail": fail, "regex": regex,
                    "timeout_ms": _integer(raw.get("timeout_ms", 30000), "wait_reply timeout_ms", 1, 3600000),
                    "save": _identifier(raw.get("save", ""), "wait_reply save")}
        elif kind == "extract":
            pattern = _pattern(raw.get("regex"), "extract regex", required=True)
            group = _integer(raw.get("group", 0), "extract group", 0, 20)
            if group > safe_regex.compile(pattern).groups:
                raise ValueError("extract group exceeds the regular expression groups")
            step = {"t": kind, "from": _destination(raw.get("from", "target"), "extract from"),
                    "regex": pattern, "group": group,
                    "save": _identifier(raw.get("save"), "extract save", empty=False)}
        else:
            step = {"t": kind, "from": _destination(raw.get("from", "target"), "captcha from"),
                    "length": _integer(raw.get("length", 4), "captcha length", 1, 16),
                    "send_to": _destination(raw.get("send_to", "target"), "captcha send_to"),
                    "template": _string(raw.get("template", ""), "captcha template", MAX_TEXT)}
        if kind == "click" and step["ai"] and any("{credential." in step[field]
                                                     for field in ("to", "text", "emoji")):
            raise ValueError("AI click steps cannot use credential variables")
        result.append(step)
    _validate_step_templates(result)
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _validate_step_templates(steps):
    variables, last_ready, captcha_ready = set(), False, False
    fields = {
        "send": ("to", "text"), "click": ("to", "text", "emoji"),
        "wait_reply": ("from",), "extract": ("from",),
        "captcha": ("from", "send_to", "template"), "wait": (),
    }
    for index, step in enumerate(steps):
        same_step_captcha = step["t"] == "captcha"
        for field in fields[step["t"]]:
            for match in _TEMPLATE.finditer(step[field]):
                name = match.group(1)
                if name == "uuid" or re.fullmatch(r"(?:num|word|alpha):\d+", name, re.I):
                    if name != "uuid" and int(name.split(":", 1)[1]) > 256:
                        raise ValueError(f"custom_steps[{index}] random placeholder is too large")
                    continue
                if name.startswith("trigger.") or name.startswith("credential."):
                    if name not in _VARIABLES:
                        raise ValueError(f"custom_steps[{index}] uses unsupported variable {name}")
                    continue
                if name == "last.text" and last_ready:
                    continue
                if name in ("captcha", "code") and (captcha_ready or same_step_captcha):
                    continue
                if name.startswith("var.") and name[4:] in variables:
                    continue
                raise ValueError(f"custom_steps[{index}] uses unavailable variable {name}")
        if step["t"] == "extract":
            source = step["from"]
            if source == "last.text" and not last_ready:
                raise ValueError(f"custom_steps[{index}] uses last.text before a reply")
            if source.startswith("trigger.") and source not in _VARIABLES:
                raise ValueError(f"custom_steps[{index}] uses an unsupported trigger variable")
            if source.startswith("var.") and source[4:] not in variables:
                raise ValueError(f"custom_steps[{index}] uses an unavailable extracted variable")
            if source in ("captcha", "code") and not captcha_ready:
                raise ValueError(f"custom_steps[{index}] uses captcha before recognition")
            variables.add(step["save"])
        elif step["t"] == "wait_reply":
            last_ready = True
            if step["save"]:
                variables.add(step["save"])
        elif step["t"] == "captcha":
            captcha_ready = True


def validate_template(value):
    _string(value, "template", MAX_TEXT)
    for match in _TEMPLATE.finditer(value):
        name = match.group(1)
        if name == "uuid":
            continue
        random_placeholder = re.fullmatch(r"(?:num|word|alpha):(\d+)", name, re.I)
        if random_placeholder:
            if int(random_placeholder.group(1)) > 256:
                raise ValueError("random placeholder length must not exceed 256")
            continue
        if name in _VARIABLES or (name.startswith("var.") and IDENTIFIER.fullmatch(name[4:])):
            continue
        raise ValueError(f"unsupported variable: {name}")
    return value


def parse_trigger(value):
    return json.loads(canonical_trigger(value))


def parse_steps(value):
    return json.loads(canonical_steps(value))


def uses_credentials(steps):
    if isinstance(steps, str):
        steps = parse_steps(steps)
    return any("{credential.user}" in value or "{credential.secret}" in value
               for step in steps for value in step.values() if isinstance(value, str))


def uses_default_target(steps):
    if isinstance(steps, str):
        steps = parse_steps(steps)
    fields = {"send": ("to",), "click": ("to",), "wait_reply": ("from",),
              "extract": ("from",), "captcha": ("from", "send_to")}
    return any(step.get(field) == "target" for step in steps for field in fields.get(step["t"], ()))


def uses_trigger_context(steps):
    if isinstance(steps, str):
        steps = parse_steps(steps)
    destination_fields = {"send": ("to",), "click": ("to",), "wait_reply": ("from",),
                          "extract": ("from",), "captcha": ("from", "send_to")}
    return any(("{trigger." in value or value.startswith("trigger.")
                or (field in destination_fields.get(step["t"], ()) and value == "source"))
               for step in steps for field, value in step.items() if isinstance(value, str))


def render_template(template, context):
    template = validate_template(template)

    def replace(match):
        name = match.group(1)
        random_placeholder = re.fullmatch(r"(?:num|word|alpha):(\d+)", name, re.I)
        if random_placeholder and int(random_placeholder.group(1)) > 256:
            raise ValueError("random placeholder length must not exceed 256")
        if name == "uuid" or random_placeholder:
            return match.group(0)
        if name.startswith("var.") and IDENTIFIER.fullmatch(name[4:]):
            source, key = context.get("var", {}), name[4:]
        elif name in _VARIABLES:
            parts = name.split(".", 1)
            source, key = (context, parts[0]) if len(parts) == 1 else (context.get(parts[0], {}), parts[1])
        else:
            raise ValueError(f"unsupported variable: {name}")
        if key not in source or source[key] is None:
            raise ValueError(f"missing variable: {name}")
        return str(source[key])

    return _TEMPLATE.sub(replace, template)


def group_matches(trigger, text, buttons):
    """Return (matched, matching button dict or None)."""
    checks = []
    if trigger["text"]:
        checks.append(_trigger_pattern(trigger["text"], text) if trigger["text_regex"]
                      else trigger["text"].lower() in text.lower())
    matching = None
    if trigger["button"]:
        for button in buttons:
            haystack = "\n".join((button.get("text", ""), button.get("url", "")))
            found = _trigger_pattern(trigger["button"], haystack) if trigger["button_regex"] else trigger["button"].lower() in haystack.lower()
            if found:
                matching = button
                break
        checks.append(matching is not None)
    return ((all(checks) if trigger["logic"] == "all" else any(checks)), matching)


def _trigger_pattern(pattern, text):
    try:
        return bool(search_pattern(pattern, text))
    except ValueError:
        return False


def search_pattern(pattern, text):
    try:
        return safe_regex.search(pattern, str(text or ""), safe_regex.I, timeout=0.05)
    except TimeoutError:
        raise ValueError("regular expression evaluation timed out") from None


def redact(text, credential=None):
    text = str(text or "")
    for secret in sorted((str(value) for value in (credential or {}).values() if value), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text
