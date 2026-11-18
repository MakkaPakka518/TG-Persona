"""Strict custom-task configuration shared by API, import, AI, and runtime."""
from datetime import datetime
import json
import re
try:
    import regex as safe_regex
except ImportError:  # Release tests install only the lightweight dependency set.
    safe_regex = None


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
            _compile_pattern(value)
        except (re.error, ValueError):
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


EXTENDED_STEP_TYPES = {"set", "if", "repeat", "while", "ai_generate"}
MAX_TOTAL_STEPS = 120
MAX_DEPTH = 4
_CONDITION_OPS = {"contains", "equals", "not_contains", "regex", "empty", "truthy"}


def _has_extended_step(steps):
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        if step.get("t") in EXTENDED_STEP_TYPES:
            return True
        for key in ("then", "else", "steps"):
            value = step.get(key)
            if isinstance(value, list) and _has_extended_step(value):
                return True
    return False


def _extended_strings(steps):
    out = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        out.extend(str(value) for value in step.values() if isinstance(value, str))
        for key in ("then", "else", "steps"):
            if isinstance(step.get(key), list):
                out.extend(_extended_strings(step[key]))
    return out


def _extended_variables(steps):
    names = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("t") in ("set", "ai_generate") and isinstance(step.get("name") or step.get("save"), str):
            names.add(step.get("name") or step.get("save"))
        if step.get("t") in ("wait_reply", "extract") and isinstance(step.get("save"), str):
            names.add(step["save"])
        for key in ("then", "else", "steps"):
            if isinstance(step.get(key), list):
                names.update(_extended_variables(step[key]))
    return names


def _validate_extended_templates(steps):
    variables = _extended_variables(steps)
    for value in _extended_strings(steps):
        for match in _TEMPLATE.finditer(value):
            name = match.group(1)
            random_placeholder = re.fullmatch(r"(?:num|word|alpha):(\d+)", name, re.I)
            if name == "uuid" or random_placeholder:
                if random_placeholder and int(random_placeholder.group(1)) > 256:
                    raise ValueError("random placeholder length must not exceed 256")
                continue
            if name in _VARIABLES or name in ("last.text", "captcha", "code"):
                continue
            if name.startswith("var.") and name[4:] in variables:
                continue
            raise ValueError(f"uses unavailable variable {name}")


def _canonical_extended_list(steps, depth, counter):
    if not isinstance(steps, list) or not 1 <= len(steps):
        raise ValueError("custom_steps must contain at least one step")
    if depth > MAX_DEPTH:
        raise ValueError("custom_steps nesting is too deep")
    result = []
    for raw in steps:
        counter[0] += 1
        if counter[0] > MAX_TOTAL_STEPS:
            raise ValueError(f"custom_steps must contain at most {MAX_TOTAL_STEPS} steps in total")
        if not isinstance(raw, dict):
            raise ValueError("custom_steps item must be an object")
        kind = raw.get("t")
        if kind in ("then", "else") or kind is None:
            raise ValueError("custom_steps item has an invalid type")
        if kind == "set":
            _object(raw, {"t", "name", "value"}, "set step")
            result.append({"t": "set", "name": _identifier(raw.get("name"), "set name", empty=False),
                           "value": _string(raw.get("value"), "set value", MAX_TEXT)})
        elif kind == "if":
            _object(raw, {"t", "left", "op", "right", "then", "else"}, "if step")
            left = _string(raw.get("left"), "if left", MAX_TEXT)
            right = _string(raw.get("right", ""), "if right", MAX_TEXT)
            op = _string(raw.get("op"), "if op", 16, empty=False)
            if op not in _CONDITION_OPS:
                raise ValueError("if op must be contains, equals, not_contains, regex, empty, or truthy")
            if op == "regex":
                _pattern(right, "if right", required=True)
            then_raw, else_raw = raw.get("then", []), raw.get("else", [])
            if not isinstance(then_raw, list) or not isinstance(else_raw, list) or not (then_raw or else_raw):
                raise ValueError("if step requires then or else steps")
            then_steps = _canonical_extended_list(then_raw, depth + 1, counter) if then_raw else []
            else_steps = _canonical_extended_list(else_raw, depth + 1, counter) if else_raw else []
            result.append({"t": "if", "left": left, "op": op, "right": right,
                           "then": then_steps, "else": else_steps})
        elif kind in ("repeat", "while"):
            if kind == "repeat":
                _object(raw, {"t", "times", "steps"}, "repeat step")
                times = _integer(raw.get("times", 1), "repeat times", 1, 100)
                result.append({"t": "repeat", "times": times,
                               "steps": _canonical_extended_list(raw.get("steps"), depth + 1, counter)})
            else:
                _object(raw, {"t", "left", "op", "right", "max", "steps"}, "while step")
                left = _string(raw.get("left"), "while left", MAX_TEXT)
                right = _string(raw.get("right", ""), "while right", MAX_TEXT)
                op = _string(raw.get("op"), "while op", 16, empty=False)
                if op not in _CONDITION_OPS:
                    raise ValueError("while op is invalid")
                if op == "regex":
                    _pattern(right, "while right", required=True)
                result.append({"t": "while", "left": left, "op": op, "right": right,
                               "max": _integer(raw.get("max", 20), "while max", 1, 100),
                               "steps": _canonical_extended_list(raw.get("steps"), depth + 1, counter)})
        elif kind == "ai_generate":
            _object(raw, {"t", "prompt", "save", "max_tokens"}, "ai_generate step")
            result.append({"t": "ai_generate", "prompt": _string(raw.get("prompt"), "ai prompt", MAX_TEXT, empty=False),
                           "save": _identifier(raw.get("save"), "ai save", empty=False),
                           "max_tokens": _integer(raw.get("max_tokens", 512), "ai max_tokens", 64, 4096)})
        else:
            result.append(_canonical_extended_legacy(raw, kind, depth, counter))
    return result


def _canonical_extended_legacy(raw, kind, depth, counter):
    if kind == "send":
        _object(raw, {"t", "to", "text", "reply"}, "send step")
        return {"t": kind, "to": _destination(raw.get("to", "target"), "send to"),
                "text": _string(raw.get("text"), "send text", MAX_TEXT, empty=False),
                "reply": _boolean(raw.get("reply", False), "send reply")}
    if kind == "wait":
        _object(raw, {"t", "ms"}, "wait step")
        return {"t": kind, "ms": _integer(raw.get("ms", 1000), "wait ms", 0, 3600000)}
    if kind == "click":
        _object(raw, {"t", "to", "text", "emoji", "index", "any", "ai", "mode", "fresh", "timeout_ms"}, "click step")
        legacy_ai = _boolean(raw.get("ai", False), "click ai")
        mode = _string(raw.get("mode", "ai" if legacy_ai else "script"), "click mode", 16)
        if mode not in ("script", "ai", "fallback"):
            raise ValueError("click mode must be script, ai, or fallback")
        if mode != "script" and any("{credential." in str(raw.get(field, "")) for field in ("to", "text", "emoji")):
            raise ValueError("AI click steps cannot use credential variables")
        return {"t": kind, "to": _destination(raw.get("to", "target"), "click to"),
                "text": _string(raw.get("text", ""), "click text", 1024),
                "emoji": _string(raw.get("emoji", ""), "click emoji", 128),
                "index": _integer(raw.get("index", 0), "click index", 0, 100),
                "any": _boolean(raw.get("any", False), "click any"),
                "ai": mode == "ai", "mode": mode,
                "fresh": _boolean(raw.get("fresh", True), "click fresh"),
                "timeout_ms": _integer(raw.get("timeout_ms", 30000), "click timeout_ms", 1, 3600000)}
    if kind == "wait_reply":
        _object(raw, {"t", "from", "success", "fail", "regex", "timeout_ms", "save"}, "wait_reply step")
        regex = _boolean(raw.get("regex", False), "wait_reply regex")
        success = _string(raw.get("success", ""), "wait_reply success", MAX_PATTERN)
        fail = _string(raw.get("fail", ""), "wait_reply fail", MAX_PATTERN)
        if regex:
            _pattern(success, "wait_reply success")
            _pattern(fail, "wait_reply fail")
        return {"t": kind, "from": _destination(raw.get("from", "target"), "wait_reply from"),
                "success": success, "fail": fail, "regex": regex,
                "timeout_ms": _integer(raw.get("timeout_ms", 30000), "wait_reply timeout_ms", 1, 3600000),
                "save": _identifier(raw.get("save", ""), "wait_reply save")}
    if kind == "extract":
        _object(raw, {"t", "from", "regex", "group", "save"}, "extract step")
        pattern = _pattern(raw.get("regex"), "extract regex", required=True)
        group = _integer(raw.get("group", 0), "extract group", 0, 20)
        if group > _compile_pattern(pattern).groups:
            raise ValueError("extract group exceeds the regular expression groups")
        return {"t": kind, "from": _destination(raw.get("from", "target"), "extract from"),
                "regex": pattern, "group": group,
                "save": _identifier(raw.get("save"), "extract save", empty=False)}
    if kind == "captcha":
        _object(raw, {"t", "from", "length", "send_to", "template"}, "captcha step")
        return {"t": kind, "from": _destination(raw.get("from", "target"), "captcha from"),
                "length": _integer(raw.get("length", 4), "captcha length", 1, 16),
                "send_to": _destination(raw.get("send_to", "target"), "captcha send_to"),
                "template": _string(raw.get("template", ""), "captcha template", MAX_TEXT)}
    raise ValueError(f"custom_steps has an invalid type {kind}")


def canonical_steps(value):
    steps = _json(value, "custom_steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 50:
        raise ValueError("custom_steps must contain 1 to 50 steps")
    if _has_extended_step(steps):
        normalized = _canonical_extended_list(steps, 0, [0])
        _validate_extended_templates(normalized)
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    result = []
    schemas = {
        "send": {"t", "to", "text", "reply"},
        "wait": {"t", "ms"},
        "click": {"t", "to", "text", "emoji", "index", "any", "ai", "mode", "fresh", "timeout_ms"},
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
            legacy_ai = _boolean(raw.get("ai", False), "click ai")
            mode = _string(raw.get("mode", "ai" if legacy_ai else "script"), "click mode", 16)
            if mode not in ("script", "ai", "fallback"):
                raise ValueError("click mode must be script, ai, or fallback")
            step = {"t": kind, "to": _destination(raw.get("to", "target"), "click to"),
                    "text": _string(raw.get("text", ""), "click text", 1024),
                    "emoji": _string(raw.get("emoji", ""), "click emoji", 128),
                    "index": _integer(raw.get("index", 0), "click index", 0, 100),
                    "any": _boolean(raw.get("any", False), "click any"),
                    "ai": mode == "ai", "mode": mode,
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
            if group > _compile_pattern(pattern).groups:
                raise ValueError("extract group exceeds the regular expression groups")
            step = {"t": kind, "from": _destination(raw.get("from", "target"), "extract from"),
                    "regex": pattern, "group": group,
                    "save": _identifier(raw.get("save"), "extract save", empty=False)}
        else:
            step = {"t": kind, "from": _destination(raw.get("from", "target"), "captcha from"),
                    "length": _integer(raw.get("length", 4), "captcha length", 1, 16),
                    "send_to": _destination(raw.get("send_to", "target"), "captcha send_to"),
                    "template": _string(raw.get("template", ""), "captcha template", MAX_TEXT)}
        if kind == "click" and step["mode"] != "script" and any("{credential." in step[field]
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


def _walk_steps(steps):
    for step in steps:
        if not isinstance(step, dict):
            continue
        yield step
        for key in ("then", "else", "steps"):
            if isinstance(step.get(key), list):
                yield from _walk_steps(step[key])


def uses_credentials(steps):
    if isinstance(steps, str):
        steps = parse_steps(steps)
    return any("{credential.user}" in value or "{credential.secret}" in value
               for step in _walk_steps(steps) for value in step.values() if isinstance(value, str))


def uses_default_target(steps):
    if isinstance(steps, str):
        steps = parse_steps(steps)
    fields = {"send": ("to",), "click": ("to",), "wait_reply": ("from",),
              "extract": ("from",), "captcha": ("from", "send_to")}
    return any(step.get(field) == "target" for step in _walk_steps(steps) for field in fields.get(step["t"], ()))


def uses_trigger_context(steps):
    if isinstance(steps, str):
        steps = parse_steps(steps)
    destination_fields = {"send": ("to",), "click": ("to",), "wait_reply": ("from",),
                          "extract": ("from",), "captcha": ("from", "send_to")}
    return any(("{trigger." in value or value.startswith("trigger.")
                or (field in destination_fields.get(step["t"], ()) and value == "source"))
               for step in _walk_steps(steps) for field, value in step.items() if isinstance(value, str))


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
    if safe_regex is None:
        _fallback_pattern_guard(pattern)
        return re.search(pattern, str(text or ""), re.I)
    try:
        return safe_regex.search(pattern, str(text or ""), safe_regex.I, timeout=0.05)
    except TimeoutError:
        raise ValueError("regular expression evaluation timed out") from None


def _compile_pattern(pattern):
    if safe_regex is not None:
        try:
            return safe_regex.compile(pattern)
        except safe_regex.error:
            raise ValueError("invalid regular expression") from None
    _fallback_pattern_guard(pattern)
    return re.compile(pattern)


def _fallback_pattern_guard(pattern):
    # Fail closed on constructs most prone to catastrophic backtracking.
    if (re.search(r"\\[1-9]|\)(?:[*+]|\{)|(?:\.\*){2}|(?:\.\+){2}", pattern)
            or len(re.findall(r"(?<!\\)[*+]", pattern)) > 2):
        raise ValueError("regular expression requires the regex safety dependency")


def redact(text, credential=None):
    text = str(text or "")
    for secret in sorted((str(value) for value in (credential or {}).values() if value), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text
