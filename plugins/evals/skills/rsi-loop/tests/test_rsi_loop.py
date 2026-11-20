"""Tests for the rsi-loop gate, experience judge and trace steps. Standard library only, no model calls.

    python3 -m unittest discover -s plugins/evals/skills/rsi-loop/tests
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB))
import analyze  # noqa: E402
import common  # noqa: E402
import gate  # noqa: E402
import report  # noqa: E402
import sample  # noqa: E402

SHOTS = json.loads((LIB.parent / "templates" / "fewshot_gate.json").read_text())

# Invented traffic. Conversation c1 is the source of the task under test; c2 is someone else's.
SOURCE = "please pull the weekly delivery status for the billing team and post a short summary before noon"
REPLY = "here is the weekly delivery status for the billing team with three items at risk and one blocked"
OTHER = "can you list every open request that has been waiting on finance approval for more than two weeks"
TURNS = [
    {"conversation_id": "c1", "turn_id": "t1", "created_time": "2026-01-05T10:00:00", "utterance": SOURCE, "bot_reply": REPLY,
     "bot_final_reply": REPLY, "next_utterance": "thanks"},
    {"conversation_id": "c2", "turn_id": "t2", "created_time": "2026-01-05T11:00:00", "utterance": OTHER, "bot_reply": "",
     "bot_final_reply": ""},
]
ROW = {"conversation_id": "c1", "turn_id": "t1", "strata": {"audience": "people", "tool_family": "Tracker", "experience": "good"},
       "poor_signals": [], "tools": ["Tracker"], "cell_population": 10}
CLEAN = "Each morning, gather how delivery is going for the payments group and share a brief recap ahead of midday."


def run_of(text, start, n):
    return " ".join(text.split()[start:start + n])


class WordingCheck(unittest.TestCase):
    def setUp(self):
        self.ix = common.WordingIndex(TURNS)

    def test_eight_words_from_own_conversation_fail(self):
        self.assertTrue(common.wording_violations(self.ix, "c1", "Could you " + run_of(SOURCE, 2, 8) + " every week?"))

    def test_eight_words_from_the_assistant_turn_fail(self):
        self.assertTrue(common.wording_violations(self.ix, "c1", "Summarize " + run_of(REPLY, 3, 8) + " for me."))

    def test_seven_words_pass(self):
        self.assertEqual(common.wording_violations(self.ix, "c1", "Could you " + run_of(SOURCE, 2, 7) + " this week?"), [])

    def test_ten_words_from_any_message_fail_nine_pass(self):
        self.assertTrue(common.wording_violations(self.ix, "c1", "Then " + run_of(OTHER, 2, 10) + " too."))
        self.assertEqual(common.wording_violations(self.ix, "c1", "Then " + run_of(OTHER, 2, 9) + " too."), [])

    def test_criteria_are_checked_too(self):
        self.assertTrue(common.wording_violations(self.ix, "c1", CLEAN, "Reports " + run_of(REPLY, 3, 9) + "."))

    def test_whole_short_message_reused(self):
        ix = common.WordingIndex(TURNS + [{"conversation_id": "c3", "utterance": "show my open approvals"}])
        self.assertTrue(common.wording_violations(ix, "c3", "Show my open approvals"))
        self.assertEqual(common.wording_violations(ix, "c3", "List the approvals waiting on me"), [])

    def test_clean_rewrite_passes(self):
        self.assertEqual(common.wording_violations(self.ix, "c1", CLEAN, "Gives a short delivery recap for that group."), [])


class FakeModel:
    """Stands in for llm.call: answers by which system prompt it was given and records what it saw."""

    def __init__(self, systems, gate_task, rewords):
        self.systems, self.gate_task, self.rewords, self.seen = systems, gate_task, list(rewords), []

    def __call__(self, system, prompt, schema, model="sonnet", **kw):
        self.seen.append((next(k for k, v in self.systems.items() if v == system), prompt))
        if system == self.systems["gate"]:
            return {"decision": "keep", "drop_reason": "none", "removed_terms": [], "task": self.gate_task,
                    "success_criteria": "Gives a short recap of delivery for that team.", "placeholders": [], "runtime_data": "none"}
        if system == self.systems["reword"]:
            return {"task": self.rewords.pop(0) if len(self.rewords) > 1 else self.rewords[0],
                    "success_criteria": "Gives a short recap of delivery for that team."}
        return {"clean": True, "issue": "none"}


class GateWordingStep(unittest.TestCase):
    def setUp(self):
        self.systems = gate.build_systems(SHOTS)
        self.models = {"gate": "m", "verify": "m", "reword": "m"}
        self.ix = common.WordingIndex(TURNS)
        self.real_call = gate.llm.call

    def tearDown(self):
        gate.llm.call = self.real_call

    def run_gate(self, fake):
        gate.llm.call = fake
        return gate.gate_one(ROW, [TURNS[0]], self.systems, self.models, self.ix)

    def test_verbatim_rewrite_is_reworded_then_kept(self):
        fake = FakeModel(self.systems, SOURCE.capitalize() + ".", [CLEAN])
        audit, task = self.run_gate(fake)
        self.assertEqual(audit["decision"], "keep")
        self.assertEqual(audit["reworded"], 1)
        self.assertEqual(task["task"], CLEAN)
        self.assertEqual(common.wording_violations(self.ix, "c1", task["task"], task["success_criteria"]), [])
        verify_inputs = [p for k, p in fake.seen if k == "verify"]
        self.assertEqual(len(verify_inputs), 1)
        self.assertIn(CLEAN, verify_inputs[0])                    # the audit sees the final rewrite only
        self.assertNotIn(run_of(SOURCE, 2, 8), verify_inputs[0])

    def test_reword_that_never_gets_clean_is_dropped(self):
        stuck = "Could you " + run_of(SOURCE, 1, 12) + " please?"
        fake = FakeModel(self.systems, stuck, [stuck])
        audit, task = self.run_gate(fake)
        self.assertIsNone(task)
        self.assertEqual((audit["decision"], audit["drop_reason"], audit["stage"]), ("drop", "source_wording", "wording"))
        self.assertEqual(sum(1 for k, _ in fake.seen if k == "reword"), gate.REWORD_TRIES)
        self.assertFalse(any(k == "verify" for k, _ in fake.seen))

    def test_reword_sees_only_the_rewritten_task(self):
        stuck = "Hi, " + SOURCE + "."
        fake = FakeModel(self.systems, stuck, [CLEAN])
        self.run_gate(fake)
        prompt = json.loads(next(p for k, p in fake.seen if k == "reword"))
        self.assertEqual(set(prompt), {"task", "success_criteria", "keep_exact", "avoid_phrases"})
        self.assertEqual(prompt["task"], stuck)

    def test_clean_rewrite_makes_no_reword_call(self):
        fake = FakeModel(self.systems, CLEAN, [CLEAN])
        audit, task = self.run_gate(fake)
        self.assertEqual((audit["decision"], audit["reworded"]), ("keep", 0))
        self.assertFalse(any(k == "reword" for k, _ in fake.seen))

    def test_final_pass_refuses_to_emit(self):
        audits = [{"conversation_id": "c1", "task_id": "t-a", "decision": "keep"}, {"conversation_id": "c1", "task_id": "t-b", "decision": "keep"}]
        tasks = [{"task_id": "t-a", "task": CLEAN, "success_criteria": "ok"},
                 {"task_id": "t-b", "task": "Please " + run_of(SOURCE, 1, 9), "success_criteria": "ok"}]
        kept = gate.refuse_reused_wording(tasks, audits, self.ix)
        self.assertEqual([t["task_id"] for t in kept], ["t-a"])
        self.assertEqual((audits[1]["decision"], audits[1]["drop_reason"], audits[1]["stage"]), ("drop", "source_wording", "final_wording"))


class FewShotRules(unittest.TestCase):
    def test_rules_are_explicit_and_sent(self):
        rules = " ".join(SHOTS["rules"]).lower()
        for must in ("invented tasks with the same intent, never the user's wording", "initiative, project and roadmap content",
                     "not one-word swaps", "unreleased roadmap content is dropped"):
            self.assertIn(must, rules)
        prompt = gate.build_systems(SHOTS)["gate"]
        for r in SHOTS["rules"]:
            self.assertIn(r, prompt)

    def test_examples_use_known_reasons_and_cover_roadmap(self):
        reasons = [e["output"]["drop_reason"] for e in SHOTS["examples"]]
        self.assertTrue(set(reasons) <= set(gate.DROP_REASONS))
        self.assertIn("unreleased_roadmap", reasons)
        self.assertTrue(any("initiative" in e["utterance"].lower() and e["output"]["decision"] == "keep" for e in SHOTS["examples"]))

    def test_keep_examples_never_reuse_the_wording_and_wrong_ones_do(self):
        for i, e in enumerate(SHOTS["examples"]):
            turns = [{"conversation_id": f"x{i}", "utterance": u} for u in e["context"] + [e["utterance"]]]
            ix = common.WordingIndex(turns)
            if e["output"]["decision"] == "keep":
                self.assertEqual(common.wording_violations(ix, f"x{i}", e["output"]["task"], e["output"]["success_criteria"]), [],
                                 f"example {i} keeps the source wording")
            if e.get("not_this"):
                self.assertTrue(common.wording_violations(ix, f"x{i}", e["not_this"]["task"]), f"example {i}: the wrong rewrite should fail")


class ExperienceJudge(unittest.TestCase):
    def tearDown(self):
        sample.llm.call = self.real

    def setUp(self):
        self.real = sample.llm.call

    def test_judge_labels_and_agreement(self):
        convs = [{"conversation_id": f"c{i}", "anchor": {"utterance": "u", "bot_final_reply": "r", "_sig": sig, "plugins": []}}
                 for i, sig in enumerate([["thumbs_down"], [], ["retried"], []])]
        cache = {c["conversation_id"]: {"conversation_id": c["conversation_id"], "utterance_type": "x", "reply_outcome": "answered"} for c in convs}
        verdicts = ["poor", "good", "good", "poor"]
        seen = []

        def fake(system, prompt, schema, model="sonnet", **kw):
            self.assertEqual(system, sample.JUDGE_SYSTEM)
            items = json.loads(prompt)
            seen.extend(items)
            return {"verdicts": [{"i": it["i"], "experience": verdicts[int(convs[it["i"]]["conversation_id"][1:])],
                                  "reason": "did_the_job", "confidence": "high"} for it in items]}
        sample.llm.call = fake
        self.assertEqual(sample.judge_all(convs, "m", cache, workers=1), 0)
        self.assertEqual([it["signals"] for it in seen], [["thumbs_down"], [], ["retried"], []])   # hard signals go in as features
        self.assertTrue(all(it["reply_outcome"] == "answered" for it in seen))
        for c in convs:
            c["poor_signals"] = c["anchor"]["_sig"]
            c["judge"] = cache[c["conversation_id"]]["judge_experience"]
            c["experience"] = c["judge"]
        ag = sample.judge_agreement(convs)
        self.assertEqual((ag["both_poor"], ag["both_good"], ag["judge_poor_signals_good"], ag["judge_good_signals_poor"]), (1, 1, 1, 1))
        self.assertEqual(ag["agreement"], 0.5)
        self.assertEqual(ag["per_signal"]["retried"], {"n": 1, "judge_poor_rate": 0.0})

    def test_rubric_names_every_reason(self):
        for r in sample.JUDGE_REASONS:
            self.assertIn(r, sample.JUDGE_SYSTEM)


def playback(turns):
    return {"tasks": [{"taskId": "k", "sessions": [{"sessionId": "s", "turns": turns}]}]}


def call(name, cid):
    return {"modelContextItemPayload": {"openaiItem": {"functionToolCall": {"name": name, "callId": cid, "type": "function_call"}}}}


class Traces(unittest.TestCase):
    def test_names_turn_and_step_of_a_failed_tool(self):
        raw = playback([
            {"startedAt": "1", "outputs": [{"messageContent": "hi", "messageType": "MESSAGE_TYPE_TEXT"}], "events": [call("load_skill", "a")]},
            {"startedAt": "2", "outputs": [], "events": [
                call("tracker_get_issue", "b"),
                {"toolResultPayload": {"toolId": "tracker", "success": False, "errorMessage": "Tracker is not connected for this user"}}]},
        ])
        self.assertEqual(analyze.went_wrong_playback(raw), {"turn": 2, "of": 2, "step": 2, "tool": "tracker_get_issue", "outcome": "not_connected"})

    def test_silent_turn(self):
        raw = playback([{"startedAt": "1", "outputs": [{"messageContent": "thinking", "messageType": "MESSAGE_TYPE_REASONING"}], "events": []}])
        self.assertEqual(analyze.went_wrong_playback(raw)["outcome"], "reasoning_only")

    def test_expired_cockpit_session_stops_and_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "work"
            for sub in ("suite", "sample", "runs"):
                (work / sub).mkdir(parents=True)
            (work / "loop.json").write_text(json.dumps({"org": "acme", "assistant": "Helper"}))
            man = [{"goal_id": f"g{i}", "task_id": f"t{i}", "strata": {"experience": "poor"}, "env_fit": "simulated", "weight": 1,
                    "run_tier": "ready"} for i in range(3)]
            common.write_jsonl(work / "suite" / "manifest.jsonl", man)
            common.write_jsonl(work / "sample" / "audit.jsonl", [{"task_id": f"t{i}", "conversation_id": f"c{i}"} for i in range(3)])
            calls = Path(d) / "calls"
            fake = Path(d) / "fake_playback.sh"
            fake.write_text(f'echo x >> "{calls}"\necho "Error: Cockpit returned 401 Unauthorized; your cockpit cookies have expired." >&2\nexit 1\n')
            os.environ["RSI_TRACE_CMD"] = f"sh {fake} {{conversation_id}}"
            try:
                out = analyze.traces(work, "2026-01-06")
            finally:
                del os.environ["RSI_TRACE_CMD"]
            self.assertEqual(out["status"], "needs_cockpit_session")
            self.assertIn("Cockpit", out["reason"])
            self.assertEqual(calls.read_text().count("x"), 1)       # stops at the first 401
            g = {"traces": out, "analysis": None}
            note = report.traces_note(g)[0][1]
            self.assertTrue(note.startswith("Needs Cockpit session."))

    def test_analysis_must_name_turn_and_step_when_traced(self):
        manifest = {"g1": {"strata": {"experience": "poor"}}}
        pk = {"packets": [{"goal_id": "g1", "went_wrong_in_traffic": {"turn": 2, "of": 2, "step": 3, "tool": "x", "outcome": "not_found"}}]}
        mode = {"id": "FM-1", "title": "t", "cause": "c", "tasks": ["g1"], "evidence": ["e"], "confidence": "high", "proposal": "P-1"}
        prop = {"id": "P-1", "title": "t", "target": "w", "before": "b", "after": "a", "expected": "x", "kind": "prompt",
                "confidence": "high", "tags": ["Correctness"]}
        doc = {"basis": "traffic_sample", "failure_modes": [mode], "proposals": [prop]}
        bad = analyze.problems(doc, manifest, None, {"Correctness"}, set(), pk)
        self.assertTrue(any("went_wrong" in b for b in bad))
        mode["went_wrong"] = [{"task": "g1", "turn": 2, "step": 3, "what": "the record lookup found nothing"}]
        self.assertEqual(analyze.problems(doc, manifest, None, {"Correctness"}, set(), pk), [])


class ReportDesign(unittest.TestCase):
    def test_design_notes_answer_the_four_questions(self):
        text = " ".join(q + " " + a for q, a in report.DESIGN_NOTES)
        for must in ("turn and the step", "Cockpit", "LLM judge", "8 words in a row", "adversarially", "Approve / Skip", "MCP tool"):
            self.assertIn(must, text)

    def test_proposals_get_approve_and_skip(self):
        html = report.to_html("t", "s", [("What to merge", [("decide", "2026-01-06", "P-1", "proposed")])])
        self.assertIn("data-cmd='rsi.sh decide 2026-01-06 P-1 approve'", html)
        self.assertIn(">Skip<", html)


if __name__ == "__main__":
    unittest.main()
