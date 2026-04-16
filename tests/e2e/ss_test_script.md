# Shoulder Surf End-to-End Test Script

## How to use

Record a simulated meeting in the SS app with **3 speakers**. You play Scott. 
Have two other people (or voice-act two distinct speakers) play Alan and Priya.

Read the dialogue below naturally — don't read it word-for-word like a script. 
Paraphrase, add filler, speak at natural pace. The extraction should be robust 
to wording variations. What matters is the **semantic content**, not exact phrasing.

**Prerequisites:**
- You (Scott) must be voice-enrolled in SS so the `(you)` marker is injected
- CQ must be deployed with the current main (post-merge stack)

---

## The Script (3 speakers, ~2 minutes)

**SCOTT:** Morning everyone. Quick intros since Priya just joined the team.
I'm Scott, the backend lead on the payments project. I'm based out of Boston.
Fair warning — I prefer async communication over meetings. Happy to move 
this to a doc if folks want. Also, for the record, I'd rather debug in Go 
than Rust any day.

**ALAN:** Hey everyone, I'm Alan. I'm the mobile lead on the same project, 
working out of Dallas. Mostly iOS.

**PRIYA:** And I'm Priya, the PM. I'll be coordinating the checkout redesign.
Okay, the big question on the table — do we build the new payment flow 
in-house, or do we use Stripe Connect?

**SCOTT:** I've looked at both options. Stripe Connect saves us about six 
months of compliance work. I say we go with Stripe Connect.

**ALAN:** Agreed. That unblocks the iOS side too. I can ship iOS checkout 
version two next sprint if the backend is ready.

**PRIYA:** One issue — we haven't scheduled the compliance review for Stripe 
Connect yet. That's a blocker.

**PRIYA:** I'll get compliance on the calendar this week.

**SCOTT:** Good. I'll write up the technical RFC by Friday. But I can't 
finalize it until compliance signs off.

**ALAN:** My iOS work depends on Scott's RFC. I'll start scaffolding the 
client once it lands.

**PRIYA:** One more thing — for the first milestone, we're staying in sandbox 
only. No production traffic yet.

**SCOTT:** Got it. Sandbox only for M1, understood.

**PRIYA:** Thanks everyone. I'll send notes.

---

## Expected Patches (what CQ should produce)

### Self-typed (require `(you)` marker — Scott must be enrolled)

| # | Type | Expected text (second-person or close) | Owner |
|---|---|---|---|
| 1 | `identity` | Based in Boston | Scott |
| 2 | `preference` | Prefers async communication over meetings | Scott |
| 3 | `preference` | Prefers debugging in Go over Rust | Scott |

If Scott is NOT enrolled (no `(you)` marker), these three should be **absent**.
That's the gating test. If they appear without enrollment, the gate is broken.

### Project

| # | Type | Expected text | Owner | Notes |
|---|---|---|---|---|
| 4 | `project` | Payments project / checkout redesign | Scott | Scott owns commitments within it |

### People

| # | Type | Expected text | Notes |
|---|---|---|---|
| 5 | `person` | Alan — mobile lead, based in Dallas | Should have `works_on` connection to project |
| 6 | `person` | Priya — PM coordinating checkout redesign | Should have `works_on` connection to project |

### Decisions

| # | Type | Expected text | Owner | Connections |
|---|---|---|---|---|
| 7 | `decision` | Use Stripe Connect for the payment flow | Scott | `parent` → project |

### Commitments

| # | Type | Expected text | Owner | Connections |
|---|---|---|---|---|
| 8 | `commitment` | Write technical RFC by Friday | Scott | `parent` → project, `depends_on` → blocker #10 |
| 9 | `commitment` | Ship iOS checkout v2 next sprint | Alan | `parent` → project, `depends_on` → commitment #8 |
| 10 | `commitment` | Schedule compliance review this week | Priya | `parent` → project, `resolves` → blocker #11 |

### Blockers

| # | Type | Expected text | Owner | Connections |
|---|---|---|---|---|
| 11 | `blocker` | Compliance review for Stripe Connect not scheduled | Priya | `parent` → project |

### Takeaways

| # | Type | Expected text | Owner | Connections |
|---|---|---|---|---|
| 12 | `takeaway` | Sandbox only for first milestone, no production traffic | Priya | `parent` → project |

---

## What to verify after extraction

### Gating checks
- [ ] Self-typed patches (#1-3) are present (Scott is enrolled)
- [ ] All self-typed patches have owner = Scott, not "Scott (you)"
- [ ] No self-typed patches for Alan or Priya

### Connection checks
- [ ] Every decision/commitment/blocker/takeaway has a `parent` connection to the project
- [ ] Scott's RFC commitment (#8) has a `depends_on` connection to the blocker (#11)
- [ ] Priya's scheduling commitment (#10) has a `resolves` connection to the blocker (#11)
- [ ] Alan's iOS commitment (#9) has a `depends_on` connection to Scott's RFC (#8)

### Quality checks
- [ ] "Prefers Go over Rust" is typed as `preference`, not `trait`
- [ ] "Based in Boston" is typed as `identity`, not `trait`
- [ ] No prompt-echo content (no "Florida Blue", "Travis", "transcription samples")
- [ ] No duplicate patches
- [ ] Person patches have meaningful context (role + location), not just names
- [ ] Total patch count is 10-12 (not 3-5, not 20+)

### Connection enforcement checks
- [ ] No orphan scoped patches (decision/commitment/blocker/takeaway without parent)
- [ ] If any patches were dropped by connection enforcement, check the CQ worker logs for `connection_enforced_dropped_patches`

### Owner gate enforcement checks
- [ ] Check CQ worker logs for `owner_gate_filtered_patches` — should be 0 (marker is present)
- [ ] Check for `owner_marker_injected_server_side` — should NOT fire if SS is injecting inline

---

## Negative test (optional but valuable)

Re-run the same script with Scott **unenrolled** (remove voice profile in SS settings).

Expected:
- [ ] Zero `trait`, `preference`, `identity` patches
- [ ] Project/person/decision/commitment/blocker/takeaway patches still present
- [ ] CQ worker logs show `owner_gate_filtered_patches` with count > 0
- [ ] Total patch count drops by ~3 (the self-typed ones)

---

## How to inspect results

```bash
# Via CQ API (replace USER_ID and TOKEN):
curl -H "Authorization: Bearer $TOKEN" \
  "https://your-cq-host/v1/quilt/$USER_ID?limit=20"

# Via CQ dashboard:
# Navigate to the user's quilt view, filter by meeting_id

# Via worker logs (docker):
docker logs contextquilt-worker 2>&1 | grep -E "owner_gate|connection_enforced|extraction_reasoning"
```

After inspection, delete the test patches to keep the quilt clean:
```bash
# Delete by meeting_id if available, or manually via dashboard
```
