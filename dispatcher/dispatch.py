#!/usr/bin/env python3
"""Pick a model for a task class from your own benchmark pack. Prints ONE line.

Usage:
  python dispatcher/dispatch.py --class implementation --data-class general
         [--pack dispatcher/pack.json] [--config dispatcher/task-classes.json]
         [--cap-left PROVIDER=FRACTION ...] [--max-age-days N] [--now ISO8601]
         [--allow-fictional]

The pick, for the task's class: quality score (0 to 100, from the pack),
weighted by price and by the fraction of your subscription cap still left
for each provider:

  value = (score / 100) * (cheapest_price / price) ** price_weight * cap_left ** cap_weight

where price is the class's input:output blend of the pack's prices,
price_weight comes from the class and cap_weight from the config. A provider
with no --cap-left given is treated as not cap-limited (1.0).

It refuses (exit 1, one REFUSED line) when:
  - the pack is older than pack_max_age_days (or --max-age-days), or dated in the future;
  - the data class is unknown, or is sensitive and no provider is approved for it
    in data_policy.allowed_providers (the default until you configure it);
  - no model in the pack has a score for the class, or none has cap left;
  - the pack is marked fictional and --allow-fictional was not given.

No model id is written in this tool or its config: every candidate comes from
the pack, so re-syncing the pack is how a newer model gets picked.
Python standard library only. Exit codes: 0 pick, 1 refused, 2 usage or file error.
"""
import argparse
import datetime
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


class Refused(Exception):
    pass


def parse_time(text):
    t = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("timestamp has no time zone: %s" % text)
    return t


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_caps(items):
    caps = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError("--cap-left wants PROVIDER=FRACTION, got %r" % item)
        provider, frac = item.split("=", 1)
        value = float(frac)
        if not 0.0 <= value <= 1.0:
            raise ValueError("--cap-left fraction must be between 0 and 1: %r" % item)
        caps[provider.strip()] = value
    return caps


def number(value, name, low=0.0):
    """A finite number >= low, or ValueError naming the setting."""
    v = float(value)
    if not math.isfinite(v) or v < low:
        raise ValueError("%s must be a finite number >= %g, got %r" % (name, low, value))
    return v


def check_pack(pack):
    for key in ("generated_at", "price_unit", "sources", "models"):
        if key not in pack:
            raise ValueError("pack is missing %r (see benchmark-pack.schema.json)" % key)
    parse_time(pack["generated_at"])
    for m in pack["models"]:
        for key in ("id", "provider", "price", "scores"):
            if key not in m:
                raise ValueError("pack model is missing %r: %s" % (key, m.get("id", "?")))
        for key in ("input_per_mtok", "output_per_mtok"):
            v = m["price"].get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError("pack model %s has a bad price.%s" % (m["id"], key))
        for cls, s in m["scores"].items():
            if s.get("source") not in pack["sources"]:
                raise ValueError("pack model %s cites an unknown source for %s: %r" % (m["id"], cls, s.get("source")))
            v = s.get("score")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 100:
                raise ValueError("pack model %s has a bad score for %s" % (m["id"], cls))


def allowed_providers(config, data_class):
    """Return None when every provider may receive the data class, else the approved set."""
    policy = config.get("data_policy")
    if not isinstance(policy, dict):
        raise Refused("no data_policy in the config; nothing is sent until you configure one")
    classes = policy.get("data_classes") or {}
    if data_class not in classes:
        raise Refused("unknown data class %r; declare it under data_policy.data_classes" % data_class)
    if classes[data_class].get("sensitive", True) is False:
        return None  # only an explicit false makes a class public; anything else is treated as sensitive
    approved = (policy.get("allowed_providers") or {}).get(data_class) or []
    if not approved:
        raise Refused("sensitive data class %r has no approved provider; list the providers that may "
                      "receive it under data_policy.allowed_providers.%s" % (data_class, data_class))
    return set(approved)


def pick(config, pack, task_class, data_class, caps, now, max_age_days):
    classes = config.get("classes") or {}
    if task_class not in classes:
        raise Refused("unknown task class %r; known: %s" % (task_class, ", ".join(sorted(classes))))
    cls = classes[task_class]

    generated = parse_time(pack["generated_at"])
    age_days = (now - generated).total_seconds() / 86400.0
    if age_days > max_age_days:
        raise Refused("stale benchmark pack: generated %s, %.1f days old, limit %s days; re-run your sync"
                      % (pack["generated_at"], age_days, max_age_days))
    if age_days < -1:
        raise Refused("benchmark pack is dated in the future (%s); check the sync and the clock"
                      % pack["generated_at"])

    approved = allowed_providers(config, data_class)
    blend = cls.get("price_blend") or {"input": 1, "output": 1}
    w_in = number(blend.get("input", 1), "price_blend.input")
    w_out = number(blend.get("output", 1), "price_blend.output")
    if w_in + w_out <= 0:
        raise ValueError("price_blend for %r needs a positive input or output weight" % task_class)
    price_weight = number(cls.get("price_weight", 0.0), "price_weight")
    cap_weight = number(config.get("cap_weight", 1.0), "cap_weight")

    cands = []
    for m in pack["models"]:
        if task_class not in m["scores"]:
            continue
        if approved is not None and m["provider"] not in approved:
            continue
        price = (m["price"]["input_per_mtok"] * w_in + m["price"]["output_per_mtok"] * w_out) / (w_in + w_out)
        cands.append({"m": m, "q": float(m["scores"][task_class]["score"]),
                      "src": m["scores"][task_class]["source"], "price": price,
                      "cap": caps.get(m["provider"], 1.0)})
    if not cands:
        why = " from an approved provider" if approved is not None else ""
        raise Refused("no model in the pack has a %r score%s" % (task_class, why))
    # A free model gets the best price factor (1.0), the same as the cheapest
    # paid one, so price can never outweigh quality without limit.
    paid = [c["price"] for c in cands if c["price"] > 0]
    cheapest = min(paid) if paid else 0.0
    for c in cands:
        c["qf"] = c["q"] / 100.0
        effective = max(c["price"], cheapest)
        c["pf"] = 1.0 if effective <= 0 else (cheapest / effective) ** price_weight
        c["cf"] = c["cap"] ** cap_weight
        c["value"] = c["qf"] * c["pf"] * c["cf"]
    cands.sort(key=lambda c: (-c["value"], -c["q"], c["price"], c["m"]["id"]))
    if cands[0]["value"] <= 0:
        raise Refused("no candidate for %r has subscription cap left" % task_class)

    win = cands[0]
    unit = pack["price_unit"]
    line = ("DISPATCH %s -> %s | score %.3f (quality %.1f from %s, pack %s) | price basis %.2f blended "
            "%g:%g input:output, %s | cap left %s %.2f"
            % (task_class, win["m"]["id"], win["value"], win["q"], win["src"], pack["generated_at"],
               win["price"], w_in, w_out, unit, win["m"]["provider"], win["cap"]))
    if len(cands) > 1:
        run = cands[1]
        reasons = []
        for key, label, fmt in (("qf", "lower quality", "%.1f vs %.1f" % (run["q"], win["q"])),
                                ("pf", "higher price", "%.2f vs %.2f" % (run["price"], win["price"])),
                                ("cf", "less cap left", "%.2f vs %.2f" % (run["cap"], win["cap"]))):
            ratio = (win[key] / run[key]) if run[key] > 0 else float("inf")
            reasons.append((ratio, label, fmt))
        ratio, label, fmt = max(reasons)
        line += " | runner-up %s %.3f: lost on %s (%s)" % (run["m"]["id"], run["value"], label, fmt)
    else:
        line += " | runner-up: none (only one eligible model)"
    return line


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pick a model for a task class from your benchmark pack.")
    ap.add_argument("--class", dest="task_class", required=True)
    ap.add_argument("--data-class", required=True, help="data class of the task's inputs, from data_policy")
    ap.add_argument("--pack", default=os.path.join(HERE, "pack.json"))
    ap.add_argument("--config", default=os.path.join(HERE, "task-classes.json"))
    ap.add_argument("--cap-left", action="append", metavar="PROVIDER=FRACTION")
    ap.add_argument("--max-age-days", type=float)
    ap.add_argument("--now", help="ISO 8601 time to judge pack age against (default: now)")
    ap.add_argument("--allow-fictional", action="store_true", help="accept a pack marked fictional (tests only)")
    a = ap.parse_args(argv)
    try:
        config = load_json(a.config)
        pack = load_json(a.pack)
        check_pack(pack)
        caps = parse_caps(a.cap_left)
        now = parse_time(a.now) if a.now else datetime.datetime.now(datetime.timezone.utc)
        max_age = number(a.max_age_days if a.max_age_days is not None else config.get("pack_max_age_days", 7),
                         "max age in days")
    except (OSError, ValueError, TypeError, AttributeError) as e:
        print("ERROR: %s" % e)
        return 2
    try:
        if pack.get("fictional") and not a.allow_fictional:
            raise Refused("the pack is marked fictional; use a pack from your own sync")
        print(pick(config, pack, a.task_class, a.data_class, caps, now, max_age))
    except Refused as e:
        print("REFUSED: %s" % e)
        return 1
    except (ValueError, TypeError, AttributeError, KeyError, ZeroDivisionError) as e:
        print("ERROR: bad config or pack: %s" % e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
