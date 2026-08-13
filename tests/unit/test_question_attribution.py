"""Who was asked, and how sure CQ is about it.

The sibling of `speaker_turn_counts`, under the same constraint: the
transcript exists once, at ingest, and nothing here can ever be
backfilled. So the parser has to be right the first time, and where it
cannot be right it has to be honest about which column the number is in.

The hard case, and the one this file exists for, is a question that
NAMES somebody who is not the addressee: "Did Marcus ever send that?"
asked of the person across the table. Reading that as an explicit
question to Marcus would put the user's follow up pressure on the wrong
person's row wearing the high confidence label, which is the one error
this design cannot absorb. The rule is that a vocative is comma
delimited and sits at an edge of the sentence; a name inside a clause is
a name being talked ABOUT.
"""

from contextquilt.services.extraction_schema import question_attribution


def test_an_explicit_vocative_at_the_start_is_explicit():
    t = (
        "[Lockridge (you)] Marcus, can you get me the vendor shortlist?\n"
        "[Marcus] Yeah, absolutely, end of next week.\n"
    )
    got = question_attribution(t)
    assert got["by_label"]["marcus"]["received_explicit"] == 1
    assert got["by_label"]["marcus"]["received_inferred"] == 0
    assert got["by_label"]["marcus"]["from_user_explicit"] == 1
    assert got["unattributed"] == 0


def test_an_explicit_vocative_at_the_end_is_explicit():
    t = (
        "[Marcus] Morning.\n"
        "[Lockridge (you)] Can you get me the vendor shortlist, Marcus?\n"
        "[Priya] I can look at it too.\n"
    )
    got = question_attribution(t)
    assert got["by_label"]["marcus"]["received_explicit"] == 1
    # Priya spoke next and did NOT collect it: an explicit addressee
    # beats the who-spoke-next guess.
    assert got["by_label"]["priya"]["received_inferred"] == 0


def test_a_greeting_in_front_of_the_name_does_not_hide_it():
    t = (
        "[Lockridge (you)] Hey Marcus, where did the shortlist land?\n"
        "[Marcus] Still with legal.\n"
    )
    assert question_attribution(t)["by_label"]["marcus"]["received_explicit"] == 1


def test_a_question_that_is_only_a_name_is_explicit():
    t = "[Lockridge (you)] Marcus?\n[Marcus] Sorry, yes.\n"
    assert question_attribution(t)["by_label"]["marcus"]["received_explicit"] == 1


def test_a_name_that_is_not_the_addressee_is_never_explicit():
    # THE false positive. Lockridge is asking Priya about Marcus. Marcus
    # is in the sentence and is not being asked anything.
    t = (
        "[Marcus] Morning, all.\n"
        "[Lockridge (you)] Did Marcus ever send that shortlist?\n"
        "[Priya] Not that I saw.\n"
    )
    got = question_attribution(t)
    assert got["by_label"]["marcus"]["received_explicit"] == 0
    assert got["by_label"]["marcus"]["received_inferred"] == 0
    # It falls through to the guess, in the column a client can distrust.
    assert got["by_label"]["priya"]["received_inferred"] == 1
    assert got["by_label"]["priya"]["received_explicit"] == 0


def test_a_leading_filler_before_a_third_party_name_stays_inferred():
    t = (
        "[Marcus] Morning, all.\n"
        "[Lockridge (you)] So, did Marcus ever send that shortlist?\n"
        "[Priya] Not that I saw.\n"
    )
    got = question_attribution(t)
    assert got["by_label"]["marcus"]["received_explicit"] == 0
    assert got["by_label"]["priya"]["received_inferred"] == 1


def test_a_vocative_and_a_third_party_in_one_question():
    # "Priya, did Marcus ever send that?" is addressed to Priya and is
    # about Marcus. The edge rule gets both right.
    t = (
        "[Marcus] Morning, all.\n"
        "[Lockridge (you)] Priya, did Marcus ever send that shortlist?\n"
        "[Priya] Not that I saw.\n"
    )
    got = question_attribution(t)
    assert got["by_label"]["priya"]["received_explicit"] == 1
    assert got["by_label"]["marcus"]["received_explicit"] == 0


def test_an_ambiguous_first_name_falls_through_rather_than_guessing():
    # Two Marcuses in the room means "Marcus" names nobody in particular.
    t = (
        "[Marcus Vale] Nothing from legal yet.\n"
        "[Marcus Rowe] Same here.\n"
        "[Lockridge (you)] Marcus, can you chase it?\n"
        "[Marcus Rowe] I will.\n"
    )
    got = question_attribution(t)
    assert got["by_label"]["marcus vale"]["received_explicit"] == 0
    assert got["by_label"]["marcus rowe"]["received_explicit"] == 0
    # The guess still applies: Marcus Rowe answered.
    assert got["by_label"]["marcus rowe"]["received_inferred"] == 1


def test_a_full_label_is_matched_when_it_is_spoken_in_full():
    t = (
        "[Marcus Vale] Legal has not come back.\n"
        "[Lockridge (you)] Marcus Vale, can you chase legal?\n"
        "[Marcus Rowe] I can pick it up.\n"
    )
    got = question_attribution(t)
    assert got["by_label"]["marcus vale"]["received_explicit"] == 1


def test_a_vocative_to_someone_who_never_speaks_is_a_known_limit():
    # The addressee vocabulary is built from SPEAKER LABELS, because
    # labels are the one trustworthy identity signal in a transcript
    # (names inside spoken text are misspelled constantly; see
    # speaker_turn_counts). A person who is in the room and never says a
    # word is therefore not addressable, and their vocative falls
    # through to the guess. Recorded as a test rather than fixed here:
    # fixing it means matching arbitrary spoken names against the
    # entity graph, which this pure function has no access to, and
    # guessing at it would put a name in the EXPLICIT column.
    t = (
        "[Lockridge (you)] Renata, can you unblock legal?\n"
        "[Priya] I will ask her.\n"
    )
    got = question_attribution(t)
    assert "renata" not in got["by_label"]
    assert got["by_label"]["priya"]["received_inferred"] == 1
    assert got["by_label"]["priya"]["received_explicit"] == 0


def test_a_question_with_no_name_goes_to_whoever_answers():
    t = (
        "[Lockridge (you)] Where did the shortlist land?\n"
        "[Marcus] With legal, still.\n"
    )
    got = question_attribution(t)
    assert got["by_label"]["marcus"]["received_inferred"] == 1
    assert got["by_label"]["marcus"]["received_explicit"] == 0
    assert got["unattributed"] == 0


def test_a_question_nobody_answers_is_unattributed():
    t = "[Lockridge (you)] Where did the shortlist land?\n"
    got = question_attribution(t)
    assert got["unattributed"] == 1
    assert got["questions_total"] == 1


def test_a_question_the_same_speaker_talks_past_is_unattributed():
    # The speaker changes the subject themselves: nobody received it.
    t = (
        "[Lockridge (you)] Where did the shortlist land?\n"
        "[Lockridge (you)] Anyway, let us do the budget.\n"
        "[Marcus] Sure.\n"
    )
    got = question_attribution(t)
    assert got["unattributed"] == 1
    assert got["by_label"]["marcus"]["received_inferred"] == 0


def test_a_rhetorical_question_the_speaker_answers_is_unattributed():
    # "Why did that slip? Because legal." answers itself, and the next
    # speaker did not receive it. Inference only fires on the questions
    # that TRAIL a turn.
    t = (
        "[Marcus] Why did that slip? Because legal has not come back.\n"
        "[Lockridge (you)] Understood.\n"
    )
    got = question_attribution(t)
    assert got["by_label"]["marcus"]["asked"] == 1
    assert got["unattributed"] == 1
    assert got["user"]["received_inferred"] == 0


def test_a_rhetorical_question_followed_by_a_real_one_splits():
    # Only the last one trails the turn, so only the last one is guessed.
    t = (
        "[Marcus] Why did that slip? Because legal has not come back. "
        "Can you push them?\n"
        "[Lockridge (you)] I will call them today.\n"
    )
    got = question_attribution(t)
    assert got["by_label"]["marcus"]["asked"] == 2
    assert got["unattributed"] == 1
    assert got["user"]["received_inferred"] == 1


def test_back_to_back_questions_from_one_speaker_both_land():
    t = (
        "[Lockridge (you)] Where did the shortlist land? Is legal the holdup?\n"
        "[Marcus] Yes, still with them.\n"
    )
    got = question_attribution(t)
    assert got["by_label"]["marcus"]["received_inferred"] == 2
    assert got["by_label"]["marcus"]["from_user_inferred"] == 2
    assert got["user"]["asked"] == 2


def test_the_two_grades_are_never_summed():
    t = (
        "[Lockridge (you)] Marcus, where did the shortlist land? "
        "And is legal the holdup?\n"
        "[Marcus] Yes.\n"
    )
    got = question_attribution(t)
    row = got["by_label"]["marcus"]
    assert row["received_explicit"] == 1
    assert row["received_inferred"] == 1
    assert "received" not in row


def test_the_user_side_is_counted_separately():
    t = (
        "[Lockridge (you)] Marcus, where did the shortlist land?\n"
        "[Marcus] With legal. Lockridge, can you push them?\n"
        "[Lockridge (you)] I will.\n"
    )
    got = question_attribution(t)
    assert got["user"]["asked"] == 1
    assert got["user"]["received_explicit"] == 1
    # The user is never a row in by_label: they have no appearance row.
    assert "lockridge" not in got["by_label"]


def test_the_user_is_identified_by_the_passed_label_too():
    # The metadata lane: no inline marker, the display name instead.
    t = (
        "[Lockridge] Marcus, where did the shortlist land?\n"
        "[Marcus] With legal.\n"
    )
    got = question_attribution(t, user_label="Lockridge")
    assert got["user"]["asked"] == 1
    assert got["by_label"]["marcus"]["from_user_explicit"] == 1
    assert "lockridge" not in got["by_label"]


def test_without_a_self_label_the_user_columns_are_null_not_zero():
    # CQ cannot tell which speaker is the user, so "the user asked this
    # person nothing" is a claim it must not make.
    t = (
        "[Priya] Marcus, where did the shortlist land?\n"
        "[Marcus] With legal.\n"
    )
    got = question_attribution(t)
    assert got["user"] is None
    assert got["by_label"]["marcus"]["from_user_explicit"] is None
    assert got["by_label"]["marcus"]["from_user_inferred"] is None
    # The certain columns are still real numbers.
    assert got["by_label"]["marcus"]["received_explicit"] == 1
    assert got["by_label"]["priya"]["asked"] == 1


def test_questions_from_someone_else_do_not_count_as_from_the_user():
    t = (
        "[Priya] Marcus, where did the shortlist land?\n"
        "[Marcus] With legal.\n"
        "[Lockridge (you)] Marcus, can you push them?\n"
        "[Marcus] I will.\n"
    )
    got = question_attribution(t)
    assert got["by_label"]["marcus"]["received_explicit"] == 2
    assert got["by_label"]["marcus"]["from_user_explicit"] == 1


def test_diarization_placeholders_never_receive_or_ask():
    t = (
        "[Speaker 3] Where did the shortlist land?\n"
        "[Lockridge (you)] Who is chasing legal?\n"
        "[Speaker 3] I am.\n"
    )
    got = question_attribution(t)
    assert got["by_label"] == {}
    assert got["questions_total"] == 1  # only the identified speaker's
    # The user's question landed on a placeholder, so nobody received it.
    assert got["unattributed"] == 1
    assert got["user"]["asked"] == 1


def test_a_placeholder_turn_blocks_inference_across_it():
    t = (
        "[Lockridge (you)] Where did the shortlist land?\n"
        "[Speaker 3] Sorry, wrong room.\n"
        "[Marcus] With legal.\n"
    )
    got = question_attribution(t)
    assert got["by_label"]["marcus"]["received_inferred"] == 0
    assert got["unattributed"] == 1


def test_statements_are_not_questions():
    t = (
        "[Lockridge (you)] I need the shortlist by Friday.\n"
        "[Marcus] Understood.\n"
    )
    got = question_attribution(t)
    assert got["questions_total"] == 0
    assert got["by_label"]["marcus"]["asked"] == 0
    assert got["user"]["asked"] == 0


def test_the_you_marker_never_reaches_a_label_key():
    t = "[Lockridge (you)] Marcus, ready?\n[Marcus] Yes.\n"
    got = question_attribution(t)
    assert all("(you)" not in k for k in got["by_label"])


def test_empty_and_junk_input_return_the_empty_shape():
    for junk in (None, "", 17, "no speaker labels at all, just prose?"):
        got = question_attribution(junk)
        assert got["by_label"] == {}
        assert got["user"] is None
        assert got["questions_total"] == 0
        assert got["unattributed"] == 0


def test_a_multi_turn_meeting_totals_correctly():
    t = (
        "[Lockridge (you)] Marcus, where did the shortlist land?\n"
        "[Marcus] Partial. I have been trying to get on Renata's calendar.\n"
        "[Lockridge (you)] Is legal the holdup?\n"
        "[Marcus] Honestly? Weeks.\n"
        "[Lockridge (you)] Priya, anything from your side?\n"
        "[Priya] All clear.\n"
    )
    got = question_attribution(t)
    assert got["user"]["asked"] == 3
    assert got["by_label"]["marcus"]["received_explicit"] == 1
    assert got["by_label"]["marcus"]["received_inferred"] == 1
    assert got["by_label"]["priya"]["received_explicit"] == 1
    # Marcus's own "Honestly?" is a question he asked, and nobody
    # answered it (the next question is explicit to Priya).
    assert got["by_label"]["marcus"]["asked"] == 1
    assert got["questions_total"] == 4
    assert got["unattributed"] == 1
