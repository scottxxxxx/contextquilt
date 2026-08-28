"""Who opened the room, who closed it, who answered its questions.

Doc 21 stage 2, first cut: the three observed-behaviour signals from the
Memory Layer Spec that are EXACT from a transcript. Nothing here asks a
model anything, so nothing here can hallucinate.

Same constraint as `speaker_turn_counts` and `question_attribution`: the
transcript is in hand exactly once, at ingest, and none of this can ever
be backfilled. Every meeting that landed before this shipped is
permanently unmeasurable on these three.
"""

import pathlib

from contextquilt.services.extraction_schema import meeting_role_signals

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKER = (ROOT / "src" / "worker.py").read_text()
MIGRATION = (ROOT / "init-db" / "42_appearance_role_signals.sql").read_text()

TRANSCRIPT = """[Ana] Morning everyone, let us start.
[Bob] Sure. Did the deploy land, Ana?
[Ana] It did, yesterday.
[Bob] Great. Anything else?
[Cara] Nothing from me.
"""


def sig(text, user_label=None):
    return meeting_role_signals(text, user_label)


def test_the_first_and_last_named_turns_are_the_open_and_the_close():
    by = sig(TRANSCRIPT)["by_label"]
    assert by["ana"]["opened"] is True
    assert by["cara"]["closed"] is True
    assert by["bob"]["opened"] is False and by["bob"]["closed"] is False


def test_a_placeholder_opener_means_NOBODY_opened_it():
    """Handing the open to the first NAMED speaker would be a guess
    wearing an exact number's clothes. Same discipline as 'only the
    IMMEDIATE next turn can be an answer'."""
    text = "[Speaker 2] uh, are we recording?\n" + TRANSCRIPT
    by = sig(text)["by_label"]
    assert all(v["opened"] is False for v in by.values())
    # and the close is unaffected, because that end is still named
    assert by["cara"]["closed"] is True


def test_a_placeholder_closer_means_nobody_closed_it():
    by = sig(TRANSCRIPT + "[Unknown] (inaudible)\n")["by_label"]
    assert all(v["closed"] is False for v in by.values())
    assert by["ana"]["opened"] is True


def test_an_answer_is_the_turn_after_someone_elses_trailing_question():
    """The same adjacency `question_attribution` grades as `inferred`,
    read from the answerer's side, so the two columns cannot disagree
    about what an answer is."""
    by = sig(TRANSCRIPT)["by_label"]
    assert by["ana"]["answers_given"] == 1     # answered Bob's deploy question
    assert by["cara"]["answers_given"] == 1    # answered Bob's "Anything else?"
    assert by["bob"]["answers_given"] == 0     # Bob asked, never answered


def test_answering_yourself_is_not_an_answer():
    text = "[Ana] Did it land?\n[Ana] It did.\n[Bob] Good.\n"
    assert sig(text)["by_label"]["ana"]["answers_given"] == 0


def test_a_turn_after_a_statement_is_not_an_answer():
    """Only a TRAILING question counts. A turn following an ordinary
    statement is just the next turn."""
    text = "[Ana] The deploy landed.\n[Bob] Good to hear.\n"
    by = sig(text)["by_label"]
    assert by["bob"]["answers_given"] == 0


def test_a_question_that_is_not_last_in_its_turn_does_not_create_an_answer():
    """`question_attribution` only infers from questions after the
    turn's last statement, and this must agree with it."""
    text = "[Ana] Did it land? Anyway the release is Friday.\n[Bob] Noted.\n"
    assert sig(text)["by_label"]["bob"]["answers_given"] == 0


def test_the_user_gets_their_own_block_and_no_label_row():
    """The user has no person_appearances row (doc 16 5.15), so their
    signals ride a separate block exactly as the question counts do."""
    text = "[Scott (you)] Let us begin.\n[Ana] Did the deploy land?\n[Scott (you)] It did.\n"
    out = sig(text)
    assert "scott" not in out["by_label"]
    assert out["user"]["opened"] is True
    assert out["user"]["answers_given"] == 1


def test_no_transcript_returns_nothing_so_the_caller_writes_NULL():
    """A confident zero would be a lie about a meeting nobody parsed."""
    for bad in ("", None, 123, "no speaker labels at all"):
        assert meeting_role_signals(bad) == {}


def test_an_all_placeholder_transcript_also_returns_nothing():
    """THIS CASE WAS MISSING and a sabotage found it. The test above
    exits at 'no turns at all'; a transcript that HAS turns but whose
    every speaker is a diarization placeholder reaches a different
    branch, and that branch is the one that decides whether a fully
    unlabelled meeting reports 'nobody did anything' or 'we do not
    know'. It must report the second. The two are indistinguishable at
    today's call site, which is exactly why the contract needs pinning
    here rather than being left to the caller's shape."""
    assert meeting_role_signals("[Speaker 2] hello\n[Unknown] hi there\n") == {}


def test_false_is_an_observation_and_absence_is_not():
    """The distinction the whole column set rests on: FALSE means the
    transcript was parsed and this person did not do it, NULL means no
    transcript was parsed. Collapsing them is the `capacities = {}`
    mistake in a new place."""
    by = sig(TRANSCRIPT)["by_label"]
    assert by["bob"]["opened"] is False        # observed, not unknown
    assert meeting_role_signals("") == {}      # unknown, not observed


# ------------------------------------------------------------------
# Wiring: one pass, one transcript, and a re-ingest is one meeting.
# ------------------------------------------------------------------

def test_the_signals_are_derived_in_the_same_one_pass_as_the_others():
    """The transcript is gone after this. A second pass to get them
    later does not exist."""
    i = WORKER.index("speaker_role_signals=meeting_role_signals(")
    window = WORKER[i - 1200:i]
    assert "speaker_turns=speaker_turn_counts(" in window
    assert "speaker_questions=question_attribution(" in window
    assert "effective_summary" in WORKER[i:i + 200], "same transcript as its siblings"


def test_a_reingest_never_clobbers_an_observed_signal_with_unknown():
    """Two ingests of one meeting are one meeting (doc 19.4). A
    re-ingest that parsed no transcript must not turn an observed TRUE
    back into unknown, and one that did parse must not turn it FALSE."""
    stmt = WORKER[WORKER.index("INSERT INTO person_appearances"):]
    stmt = stmt[:stmt.index('"""')]
    for col in ("opened_meeting", "closed_meeting"):
        block = stmt[stmt.index(f"{col} = CASE"):]
        block = block[:block.index("END")]
        assert f"WHEN EXCLUDED.{col} IS NULL THEN person_appearances.{col}" in block
        assert "OR EXCLUDED." in block, "booleans OR, they do not GREATEST"
    ans = stmt[stmt.index("answers_given = CASE"):]
    ans = ans[:ans.index("END")]
    assert "GREATEST" in ans, "a count keeps the max, it never sums"


def test_the_columns_are_nullable_so_unknown_stays_expressible():
    assert "ADD COLUMN IF NOT EXISTS opened_meeting  BOOLEAN" in MIGRATION
    assert "ADD COLUMN IF NOT EXISTS closed_meeting  BOOLEAN" in MIGRATION
    assert "ADD COLUMN IF NOT EXISTS answers_given   INTEGER" in MIGRATION
    assert "NOT NULL" not in MIGRATION.split("ALTER TABLE")[1].split("COMMENT")[0]


def test_the_migration_states_what_it_does_not_claim():
    """`answers_given` is NOT the spec's terminal-answerer-from-outside-
    their-team: CQ has no team model, and 'terminal' needs to know when a
    topic closed. A served name may assert only what was observed."""
    low = MIGRATION.lower()
    assert "terminal" in low and "team model" in low
