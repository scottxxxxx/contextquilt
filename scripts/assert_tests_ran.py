"""Fail when a test run passed without actually running the tests.

WHY THIS EXISTS. A test command that selects nothing SUCCEEDS. It prints
a summary nobody reads and an exit code that means "no failures", which
is not the same thing as "no failures were possible". ShoulderSurf hit
the sharpest form of it on 2026-08-22: a suite filter that matched
nothing produced `Executed 0 tests, with 0 failures` above a success
banner, and then a deliberate sabotage of the code under test ALSO
produced no failures. Two signals that appeared to corroborate each other
("tests pass, but they are weak") and were the same emptiness seen from
opposite sides. They caught it only by refusing to accept a clean
sabotage run.

Their own gate had the same hole, and its header recorded that it existed
because a suite "was never red, it was never RUN". A gate without a count
check silently reproduces the failure it was written to prevent.

WHAT THIS ASSERTS, AND WHY IT IS THE EXECUTED COUNT RATHER THAN THE
COLLECTED ONE. pytest already fails a genuinely empty selection: a bad
path exits 4, a filter matching nothing exits 5, and CI treats both as
failures. The hole pytest leaves open is different and quieter: **every
test SKIPPING exits 0.** This suite skips DB-gated tests behind a
TEST_DATABASE_URL guard by design, so a guard that broadened, an env var
that changed name, or a fixture that started raising SkipTest would turn
the whole run green with nothing exercised, and every signal available to
a reader would say the suite passed.

So the floor is on tests that actually RAN. Collected-minus-skipped is
the only number that cannot be satisfied by a suite that did nothing.

ON THE FLOOR ITSELF. It is deliberately close to the real count rather
than generously below it. A floor with room in it tolerates exactly the
silent shrink it exists to catch. If a drop is intentional, lower the
floor IN THE SAME COMMIT: that way a shrinking test suite is always a
decision somebody made and signed, rather than something that happened.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

# Raise this when the suite grows. Lower it only in the same commit as
# the removal that made it necessary, so the drop is reviewable.
MIN_EXECUTED = 1750

# THE FLOOR IS PER SUITE, and it took a red CI step to notice. This
# script was written for the whole unit run and its floor is that run's
# size. Pointed at the DB-gated suite, sixteen tests, it failed for the
# only reason a count check can fail wrongly: the number belonged to a
# different population. The check was correct and the question was not
# the one being asked, which is the shape doc 19 keeps recording.
#
# So the floor is an argument. It still defaults to the unit-suite
# number, so no existing caller changes behaviour.


def main(path: str, minimum: int = MIN_EXECUTED) -> int:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        # No report at all is the loudest possible version of the thing
        # this script is for: the run did not happen.
        print(f"assert_tests_ran: cannot read {path}: {exc}")
        return 1

    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        print(f"assert_tests_ran: no <testsuite> in {path}")
        return 1

    def total(attr: str) -> int:
        return sum(int(s.get(attr) or 0) for s in suites)

    collected = total("tests")
    skipped = total("skipped")
    executed = collected - skipped
    print(
        f"assert_tests_ran: collected={collected} skipped={skipped} "
        f"executed={executed} floor={minimum}"
    )
    if executed < minimum:
        print(
            f"assert_tests_ran: FAIL. {executed} tests actually ran, which is "
            f"below the floor of {minimum}. Either something stopped the "
            "suite from running (a renamed path, a broadened skip guard, a "
            "fixture raising SkipTest) or tests were removed. If the removal "
            "was deliberate, lower MIN_EXECUTED in this file in the same "
            "commit."
        )
        return 1
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    path = args[0] if args else "unit-results.xml"
    floor = int(args[1]) if len(args) > 1 else MIN_EXECUTED
    sys.exit(main(path, floor))
