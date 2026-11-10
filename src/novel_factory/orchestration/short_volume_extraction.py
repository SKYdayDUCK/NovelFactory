"""Finite, text-driven TEST_FIXTURE extraction for the tide-gate short volume.

This parser deliberately does not import the prose vectors, read a ScenePlan,
or take expected balances/lifecycle from the coordinator. It captures values
from the delivered immutable text. The normal NarrativeService compares those
values to the world. Event-quantity and membership claims are also matched to
the actual accepted event effects, not merely to an event identity.

This is a bounded Chinese grammar, not general semantic understanding. Missing
required anchors or unrecognised mentions in its critical vocabulary produce
UNCERTAIN assertions and therefore block acceptance. Nonassertive references
(intentions, questions, rules, historical plans and explicitly negated repeats)
are handled separately; they do not create new world effects.
"""
from __future__ import annotations

import re
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from novel_factory.domain.reconciliation import span_for
from novel_factory.fixtures import entity_value, point
from novel_factory.orchestration.short_volume_fixture import NAMES

VERSION = "tide-finite-text-extractor-v1"
NUMBER = r"[零〇一二两三四五六七八九十百千0-9]+"
NAME = r"[\u4e00-\u9fff]{2,4}?"
CRITICAL = re.compile(r"铜(?:汛)?印|" + NUMBER + r"袋|正式成员关系|最后认知|抵达")


def quantity(token):
    """Decode the small integer grammar, without any expected story quantity."""
    if token.isascii() and token.isdigit():
        return str(int(token))
    digit = "[一二两三四五六七八九]"
    tens = rf"(?:十{digit}?|{digit}十{digit}?|{digit})"
    hundreds = rf"{digit}百(?:零{digit}|{digit}十{digit}?)?"
    thousands = rf"{digit}千(?:零{tens}|{hundreds})?"
    if not re.fullmatch(rf"(?:零|〇|{tens}|{hundreds}|{thousands})", token):
        raise ValueError("Ambiguous or unsupported Chinese integer")
    digits = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    units = {"十": 10, "百": 100, "千": 1000}
    total = current = 0
    for character in token:
        if character in digits:
            current = digits[character]
        elif character in units:
            total += (current or 1) * units[character]
            current = 0
        else:
            raise ValueError("Quantity is outside the finite fixture grammar")
    return str(total + current)


class FixtureTextExtractor:
    def __init__(self, text, scene_index, ids, event_sources):
        self.text, self.index, self.ids, self.sources = text, scene_index, ids, event_sources
        self.assertions, self.observations, self.covered = [], [], []
        self.names = {value: ids[key] for key, value in NAMES.items()}
        self.at = point(scene_index + 2, 2)

    def check(self, subject, predicate, value, *, actor=None, at=None):
        return {"check_id": str(uuid4()), "plane": "BELIEF" if actor else "WORLD",
            "subject_id": self.ids[subject], "predicate": predicate, "at": at or self.at,
            "expected_status": "VALUE", "expected_value": value,
            "actor_id": actor, "known_at": (at or self.at) if actor else None}

    def assertion(self, start, end, summary, *, source=None, check=None, classification=None):
        event = self.sources[source]["event_ref"] if source else None
        value = {"assertion_id": str(uuid4()),
            "classification": classification or "EXISTING_EVENT_PRESENTATION",
            "span": span_for(self.text, start, end), "summary": summary,
            "event_ref": event, "world_check": check, "knowledge_use": None,
            "proposed_command": None, "disclosure_id": None, "declared_delta_index": None}
        self.assertions.append(value)
        return value

    def recognise(self, name, pattern, callback, *, required=True):
        # Recognise complete clauses, not affirmative substrings hidden inside
        # negated/qualified clauses such as “并非北仓留下七十袋”.
        clause = r"(?<![\u4e00-\u9fff0-9])(?:" + pattern + r")(?=$|[，。；：！？“”\n])"
        matches = list(re.finditer(clause, self.text))
        if required and not matches:
            self.assertion(0, len(self.text), "TEST_FIXTURE未识别必需事实锚点：" + name,
                           classification="UNCERTAIN")
            self.observations.append({"rule": name, "status": "MISSING_REQUIRED_ANCHOR"})
        for match in matches:
            self.covered.append(match.span())
            start = len(self.assertions)
            try:
                parsed = callback(match)
            except (KeyError, ValueError):
                self.assertion(*match.span(), "TEST_FIXTURE无法解析事实锚点：" + name,
                               classification="UNCERTAIN")
                parsed = {"status": "UNRECOGNISED_VALUE"}
            self.observations.append({"rule": name, "quote": match.group(),
                "span": span_for(self.text, *match.span()), "parsed": parsed,
                "assertion_ids": [a["assertion_id"] for a in self.assertions[start:]]})

    def resource(self, match, account, source):
        amount = quantity(match["quantity"])
        value = {"kind": "RESOURCE_BALANCE", "account_id": self.ids[account],
            "resource_id": self.ids["grain"], "quantity": amount, "unit": "bag"}
        self.assertion(*match.span(), "原文库存数量：" + amount,
            source=source, check=self.check(account, "resource:" + self.ids["grain"] + ":bag", value))
        return {"kind": "RESOURCE_BALANCE", "account": account, "quantity": amount,
                "event_role": "TEMPORAL_ANCHOR_OR_PRIOR_EFFECT"}

    def transfer(self, match, source="joint"):
        amount = quantity(match["quantity"])
        effects = self.sources[source]["payload"]["effects"]
        matches = all(any(effect.get("kind") == "RESOURCE_DELTA"
            and effect.get("account_id") == self.ids[account]
            and effect.get("resource_id") == self.ids["grain"] and effect.get("unit") == "bag"
            and Decimal(effect["quantity"]) == sign * Decimal(amount) for effect in effects)
            for account, sign in (("warehouse", -1), ("relief", 1)))
        self.assertion(*match.span(), "原文交付数量与已接受发生的双边资源效果逐项核对：" + amount,
            source=source, classification="EXISTING_EVENT_PRESENTATION" if matches else "HISTORICAL_CONFLICT")
        return {"kind": "RESOURCE_TRANSFER", "quantity": amount, "event_effect_match": matches}

    def lifecycle(self, match, source="joint"):
        raw = match["status"]
        if raw in {"被压毁了", "已被登记为毁坏", "依旧毁坏", "已经毁坏"}:
            status = "DESTROYED"
        elif raw in {"仍完好", "仍然完好", "依旧完好", "完好无损", "已被登记为完好"}:
            status = "ACTIVE"
        else:
            raise ValueError("Unknown lifecycle phrase")
        self.assertion(*match.span(), "原文物品生命周期：" + status, source=source,
            check=self.check("sword", "lifecycle", {"kind": "STRING", "value": status}))
        return {"kind": "LIFECYCLE", "value": status}

    def gift(self, match):
        giver, recipient = self.names[match["giver"]], self.names[match["recipient"]]
        self.assertion(*match.span(), "原文赠出者在交付前持有铜印", source="gift",
            check=self.check("sword", "possessed_by", entity_value(giver), at=point(1, 2)))
        self.assertion(*match.span(), "原文赠与接收者", source="gift",
            check=self.check("sword", "possessed_by", entity_value(recipient)))
        return {"giver_id": giver, "recipient_id": recipient}

    def own_belief(self, match):
        actor = self.ids["protagonist"]  # This grammar supports this explicit POV only.
        self.assertion(*match.span(), "本场明确自我观察只进入林澈的认知切面", source="own_belief",
            check=self.check("sword", "possessed_by", entity_value(actor), actor=actor))
        return {"actor_id": actor, "possessor_id": actor, "plane": "BELIEF"}

    def old_belief(self, match):
        actor, possessor = self.names[match["actor"]], self.names[match["possessor"]]
        self.assertion(*match.span(), "原文最后认知按角色切面核对，不晋升为世界真相", source="belief",
            check=self.check("sword", "possessed_by", entity_value(possessor), actor=actor))
        return {"actor_id": actor, "possessor_id": possessor, "plane": "BELIEF"}

    def arrival(self, match):
        place = {"北岸潮闸": self.ids["north"], "南埠": self.ids["south"]}[match["place"]]
        self.assertion(*match.span(), "苏弦实际抵达原文所指位置", source="travel",
            check=self.check("rival", "located_in", entity_value(place)))
        return {"actor_id": self.ids["rival"], "destination_id": place}

    def membership(self, match):
        subject, faction = self.names[match["member"]], self.names[match["faction"]]
        matches = any(effect.get("kind") == "RELATION_TRANSITION" and effect.get("operation") == "OPEN"
            and effect.get("predicate") == "member_of" and effect.get("subject_id") == subject
            and effect.get("object_value") == entity_value(faction)
            for effect in self.sources["membership"]["payload"]["effects"])
        self.assertion(*match.span(), "原文新增成员与实际接受的关系 OPEN 效果核对",
            source="membership", classification="EXISTING_EVENT_PRESENTATION" if matches else "HISTORICAL_CONFLICT")
        return {"subject_id": subject, "faction_id": faction, "event_effect_match": matches}

    def nonassertive(self, modality):
        def record(match):
            self.assertion(*match.span(), "TEST_FIXTURE非当前事实引用：" + modality,
                           classification="SURFACE_DETAIL")
            return {"modality": modality, "creates_fact": False}
        return record

    def extract(self):
        n = "(?P<quantity>" + NUMBER + ")"
        if self.index == 0:
            self.recognise("gift", "(?P<giver>" + NAME + ")把铜汛印赠给(?P<recipient>" + NAME + ")时", self.gift)
            self.recognise("own_belief", r"她看见铜汛印已经交到自己手里", self.own_belief)
            self.recognise("incidental_handling", r"只把铜印换了个方向收好", self.nonassertive("INCIDENTAL_HANDLING"))
        elif self.index == 1:
            self.recognise("initial_warehouse", "北仓的账面仍是" + n + "袋粮", lambda m: self.resource(m, "warehouse", "travel"))
            self.recognise("initial_relief", "救济账上是" + n + "袋", lambda m: self.resource(m, "relief", "travel"))
            self.recognise("old_belief", "(?P<actor>" + NAME + ")对铜汛印的最后认知，仍是它在(?P<possessor>" + NAME + ")手中", self.old_belief)
            self.recognise("arrival", r"她沿着尚未受阻的路线抵达(?P<place>北岸潮闸|南埠)", self.arrival)
            self.recognise("requested_aid", "她要争取" + n + "袋", self.nonassertive("INTENTION"))
            self.recognise("future_balance_question", "北仓能否仍留" + n + "袋", self.nonassertive("QUESTION"))
        elif self.index == 2:
            self.recognise("seal_state", r"(?:^|(?<=[。\n]))铜汛印(?P<status>[^。；，\n]+)", self.lifecycle)
            self.recognise("transfer", "联合裁决只批准从北仓向南埠救济账交付" + n + "袋粮", self.transfer)
            self.recognise("warehouse_after", "北仓留下" + n + "袋", lambda m: self.resource(m, "warehouse", "joint"))
            self.recognise("relief_after", "南埠得到" + n + "袋", lambda m: self.resource(m, "relief", "joint"))
            self.recognise("copper_rule", r"但受力变形的铜汛印必须报废", self.nonassertive("CONDITIONAL_WORLD_RULE"))
            self.recognise("no_creation", r"没有一袋因谁先开口而多出来", self.nonassertive("NO_ADDITIONAL_EFFECT"))
        elif self.index == 3:
            self.recognise("observed_seal_state", r"林澈在复核记录中看见铜汛印(?P<status>[^。；，\n]+)", lambda m: self.lifecycle(m, "observation"))
            self.recognise("acknowledged_transfer", "承认毁印和" + n + "袋交付", self.transfer)
            self.recognise("failed_prior_plan", r"原先那条“保全铜汛印，再凭印申请入社”的安排失效了", self.nonassertive("PRIOR_PLAN_REFERENCE"))
        elif self.index == 4:
            self.recognise("membership", "(?P<faction>" + NAME + ")的名册新增了(?P<member>" + NAME + ")的正式成员关系", self.membership)
            self.recognise("retained_stock", "那" + n + "袋余粮仍然在那里", lambda m: self.resource(m, "warehouse", "joint"))
            self.recognise("same_delivery", "苏弦那边得到的仍是同一批" + n + "袋", lambda m: self.resource(m, "relief", "joint"))
            self.recognise("negated_repeat", "不会让南埠再收到" + n + "袋", self.transfer)
        elif self.index == 5:
            self.recognise("final_stock", "北仓仍有" + n + "袋粮", lambda m: self.resource(m, "warehouse", "joint"))
            self.recognise("final_relief", "南埠救济账仍是" + n + "袋", lambda m: self.resource(m, "relief", "joint"))
            self.recognise("final_seal", r"(?:^|(?<=[。\n]))铜汛印(?P<status>[^。；，\n]+)", self.lifecycle)
            self.recognise("meaning", r"那件铜印的意义不必全被新的安排继承", self.nonassertive("LITERARY_INTERPRETATION"))
            self.recognise("sound", r"没有铜印碰在腕骨上的声响", self.nonassertive("SENSORY_DESCRIPTION"))
        else:
            self.assertion(0, len(self.text), "TEST_FIXTURE不支持该场景", classification="UNCERTAIN")

        # Every mention in the finite material vocabulary must be understood.
        # New/changed critical clauses cannot disappear into surface decoration.
        for match in CRITICAL.finditer(self.text):
            if not any(start <= match.start() and match.end() <= end for start, end in self.covered):
                self.assertion(*match.span(), "TEST_FIXTURE未识别的关键事实或模态；必须修订或由独立抽取器处理",
                               classification="UNCERTAIN")
                self.observations.append({"rule": "critical_vocabulary", "status": "UNRECOGNISED_CRITICAL_SPAN",
                    "quote": match.group(), "span": span_for(self.text, *match.span())})
        return self.assertions, {"execution_kind": "TEST_FIXTURE", "extractor_version": VERSION,
            "input_text_sha256": sha256(self.text.encode()).hexdigest(), "scene_index": self.index,
            "observations": self.observations,
            "scope": "Finite quantity/lifecycle/possession/cognition/location/membership grammar; not general NLP"}


def extract_fixture_assertions(text, scene_index, ids, event_sources):
    return FixtureTextExtractor(text, scene_index, ids, event_sources).extract()
