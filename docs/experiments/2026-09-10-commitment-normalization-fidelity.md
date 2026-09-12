# Normalization fidelity: 49 commitments to mark

**One question per row: does the normalized form say the same thing the
original said?** Not whether it reads better, not whether you like the
shape. A commitment is a record of what somebody said they would do, and
a rewrite that misstates it is worse than the duplicate problem this was
meant to help with.

Run of 2026-09-10, Sonnet, purpose-slot prompt, **no splitting**.
4 of 49 were refused rather than rewritten.

## This replaces an earlier sheet, and why

The first run split a record that contained two things into two records.
Scott caught it while reviewing. That rule came from ASD-STE100's "one
instruction per sentence", which is a rule for AUTHORING documentation
where the writer owns the content. These are records of what somebody
said, so splitting asserts a structure the speaker never gave and
creates an obligation nobody stated separately. It also worked against
the goal, since the point is fewer and clearer action items, not more.

**It was also flattering the measurement.** Splitting raised the
Portuguese duplicate's similarity from 0.304 to 0.595 partly by removing
the "and Apple archive submission" tail from one side. Without splitting
it is 0.490. The gain came from discarding content, and reporting it as
evidence for normalization would have been evidence for truncation.

## What the numbers say, so this sheet is read in context

| pair | before | after |
|---|---|---|
| Portuguese (duplicate) | 0.304 | 0.490 |
| Steven (duplicate) | 0.338 | 0.347 |
| Cigna 9ab83c51 / 3ddab0d8 (separate) | 0.244 | 0.269 |
| Cigna 9ab83c51 / ce9ceed2 (separate) | 0.166 | 0.171 |
| Steven control (separate) | 0.243 | 0.223 |

Duplicates move up, separate work does not. But the harder duplicate
lands at 0.347 against a 0.35 floor, so **trigram alone still cannot
carry this** and no threshold should be built on these five points.

## What counts as NOT faithful

- It states a deadline, an owner or a detail the original did not.
- It drops a scope qualifier ("P1", "phase one", "QA", "v2.0", a named
  document) that distinguished this work from adjacent work.
- It drops half of a two-part commitment instead of keeping both.
- It changes who does the work, or what the work is for.

A REFUSED row is the normalizer declining rather than misstating. Mark
those `yes` if refusing was right, `no` if it should have managed one.

**Any single `no` that invents or drops a fact is disqualifying on its
own**, regardless of the tally. This text would sit beside a commitment
you still owe.

Known cosmetic defect, not a fidelity question: some rows read "Jitendra
update" or "Scott complete" instead of "Scott follows up". Verb
agreement, not meaning. Ignore it here.

---

### 1. `477d19ff`

- **ORIGINAL:** Update Excel sheet with latest test data link and new mobile number for testing; ensure test data covers all scenarios with proper authentication details
- **NORMALIZED:** Jitendra update Excel sheet with latest test data link and new mobile number for testing, to ensure test data covers all scenarios with proper authentication details.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 2. `d50af253`

- **ORIGINAL:** Coordinate with Vishnu and Michael to review and validate Gym before mid-week publication decision
- **NORMALIZED:** (REFUSED)

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 3. `4ec8c54f`

- **ORIGINAL:** Brian to work with Trey (Bass Pro PM) to get formal CRD requirements drafted.
- **NORMALIZED:** (REFUSED)

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 4. `4f23a965`

- **ORIGINAL:** Lava to reach out to Gearsh (Artemis platform team, reporter to Robbie) to align on external contact center integration approach and get feature request prioritized before presenting to Epson.
- **NORMALIZED:** Lava contact Gearsh (Artemis platform team, reporter to Robbie) to align on external contact center integration approach and get feature request prioritized before presenting to Epson, by mid next week.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 5. `235356ae`

- **ORIGINAL:** Work with Gamia to resolve FD owner assignment visibility and data gaps in Asana for AMD and other projects
- **NORMALIZED:** (REFUSED)

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 6. `84b99a40`

- **ORIGINAL:** Complete Portuguese language support implementation and Apple archive submission
- **NORMALIZED:** Scott complete Portuguese language support implementation and Apple archive submission, by end of weekend.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 7. `23721690`

- **ORIGINAL:** Steven will visit his mother this weekend to test ShoulderSurf in a real interview call
- **NORMALIZED:** Steven Williams visits his mother, to test ShoulderSurf in a real interview call, by this weekend.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 8. `b133913f`

- **ORIGINAL:** Steven will travel to his mother's location in the next 2 weeks to conduct interviews and gather data for app testing.
- **NORMALIZED:** Steven Williams travel to his mother's location, to conduct interviews and gather data for app testing, by 2 weeks from now.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 9. `6dd69e85`

- **ORIGINAL:** Measure Nugget's food intake using a food scale and verify that fresh pet portions stay at 8 ounces total per day (approximately 4-5 ounces per meal, twice daily), eliminating all supplemental ground beef and chicken.
- **NORMALIZED:** (REFUSED)

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 10. `f5f95a6b`

- **ORIGINAL:** Send Justin a video walkthrough on how to sign up for Original Medicare online.
- **NORMALIZED:** Scott send Justin a video walkthrough on how to sign up for Original Medicare online, by after this call.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 11. `20b8f0ab`

- **ORIGINAL:** Reach out to ServiceNow for HR/health intake integration details.
- **NORMALIZED:** Tyler contacts ServiceNow, for HR/health intake integration details.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 12. `3ddab0d8`

- **ORIGINAL:** Create test scenarios and demo scripts for P2 and P3 Cigna use cases (RX Management, Pharmacy Retail Location) to help Karthik scope work
- **NORMALIZED:** Scott creates test scenarios and demo scripts for P2 and P3 Cigna use cases (RX Management, Pharmacy Retail Location), to help Karthik scope work.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 13. `ce9ceed2`

- **ORIGINAL:** Provide script for P1 Cigna scenarios (Eligibility, Pricing, Order, Status) in the Cigna scenarios document
- **NORMALIZED:** Scott delivers script for P1 Cigna scenarios (Eligibility, Pricing, Order, Status) in the Cigna scenarios document.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 14. `9be549ef`

- **ORIGINAL:** Set up separate call for scope of work extension discussion
- **NORMALIZED:** Suresh schedules separate call, for scope of work extension discussion, by next week.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 15. `c8686931`

- **ORIGINAL:** Send approval email confirming version 2.2.2 readiness once all fixes are complete
- **NORMALIZED:** Sukumar sends approval email confirming version 2.2.2 readiness, after fixes complete.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 16. `d8df2da4`

- **ORIGINAL:** Check with team on ticket 73153 status and provide update
- **NORMALIZED:** Hrishikesh investigates ticket 73153 status and shares update, by soon.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 17. `551d6866`

- **ORIGINAL:** Test Happy Path feature and share feedback on conversational intelligence, language, and behavior
- **NORMALIZED:** Scott tests Happy Path feature and shares feedback on conversational intelligence, language, and behavior, by today.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 18. `9ab83c51`

- **ORIGINAL:** Build a new spreadsheet with Cigna demo test scenarios (priority 1 and 2 intents) paired with sample test data, with explanations at appropriate detail level for dev team to build from.
- **NORMALIZED:** Scott creates new spreadsheet with Cigna demo test scenarios (priority 1 and 2 intents) paired with sample test data, with explanations at appropriate detail level for dev team to build from, by Monday.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 19. `0a6a2cae`

- **ORIGINAL:** Follow up with team via email on user access status and confirm whether app developer or supervisor role is required for support team to debug autonomous agent issues.
- **NORMALIZED:** Hrishkesh contacts team via email on user access status and confirms whether app developer or supervisor role is required for support team to debug autonomous agent issues, by next business day.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 20. `745cecc8`

- **ORIGINAL:** Gather change management communication about rollout to 1500 users in production and forward to UIMT.
- **NORMALIZED:** Suresh gathers change management communication about rollout to 1500 users in production and forwards to UIMT, by today.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 21. `6a65e14a`

- **ORIGINAL:** Vijay and Pallavi to validate MCP endpoint and confirm it works before 2 PM
- **NORMALIZED:** Vijay validates MCP endpoint and confirms it works, by today before 2 PM.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 22. `28c5a290`

- **ORIGINAL:** Test stateless implementation and report any issues or blockers
- **NORMALIZED:** Vijay tests stateless implementation and reports any issues or blockers, by today (July 30) or tomorrow (July 31).

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 23. `ef3e0244`

- **ORIGINAL:** Vijay to push remaining CTS IT service test changes to QA to unblock CTS asset management deployment.
- **NORMALIZED:** Vijay pushes remaining CTS IT service test changes to QA, to unblock CTS asset management deployment.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 24. `b585a844`

- **ORIGINAL:** Draft and refine sample demo scenarios in a Google Doc, including hard scenarios that will impress CIGNA, with focus on scenarios like refill-too-soon, vacation fill, address change, proxy calls, and complex multi-intent handling
- **NORMALIZED:** Scott creates draft and refined sample demo scenarios in a Google Doc, including hard scenarios that will impress CIGNA, with focus on scenarios like refill-too-soon, vacation fill, address change, proxy calls, and complex multi-intent handling, by this week.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 25. `fe864ed5`

- **ORIGINAL:** Apply additional pressure on screenshot-based troubleshooting ticket to get it recognized as a blocker for July 15th release.
- **NORMALIZED:** Suresh applies additional pressure on screenshot-based troubleshooting ticket, to get it recognized as a blocker for July 15th release, as soon as possible.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 26. `7b1d6919`

- **ORIGINAL:** Continue following up with product team on history/environment isolation feature request scheduling; expect update by next week at earliest
- **NORMALIZED:** Scott continues following up with product team on history/environment isolation feature request scheduling, by next week.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 27. `6313abf7`

- **ORIGINAL:** Check with team on cross-platform issue coordination and get back with status.
- **NORMALIZED:** Pallavi checks with team on cross-platform issue coordination and gets back with status, by soon.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 28. `a6f4792c`

- **ORIGINAL:** Steven's mother will send over the psychological evaluation forms/questionnaires she uses in interviews so they can be integrated into the intake form or decision tree.
- **NORMALIZED:** Steven Williams sends psychological evaluation forms/questionnaires she uses in interviews, to be integrated into the intake form or decision tree, by today or tomorrow.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 29. `bde5edbc`

- **ORIGINAL:** Pallavi to test incident creation and verify category/subcategory/issue type are being assigned correctly in tickets.
- **NORMALIZED:** Pallavi tests incident creation and verifies category/subcategory/issue type are being assigned correctly in tickets, during ongoing testing.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 30. `db45b39d`

- **ORIGINAL:** Deliver Portuguese language support by end of weekend.
- **NORMALIZED:** Scott delivers Portuguese language support, by end of weekend.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 31. `a1fdc506`

- **ORIGINAL:** Update Scott's chart to note that biopsy results can be left as a detailed voicemail; will call either way once results are available.
- **NORMALIZED:** Hannah Westlake updates Scott's chart to note that biopsy results can be left as a detailed voicemail and will call either way once results are available.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 32. `b42092bb`

- **ORIGINAL:** Send email to Pallavi, Vijay, Sukumar, and Srikanth with response messaging document and CORS configuration issue details
- **NORMALIZED:** Joy sends email to Pallavi, Vijay, Sukumar, and Srikanth with response messaging document and CORS configuration issue details, by after call.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 33. `825a0d3d`

- **ORIGINAL:** Update attachment download implementation to use message ID and source file ID in correct order (message ID / source file ID) instead of file ID.
- **NORMALIZED:** Srikanth updates attachment download implementation to use message ID and source file ID in correct order (message ID / source file ID) instead of file ID.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 34. `9d82d3fb`

- **ORIGINAL:** Deliver AI routing endpoint (phase 1) ready for integration with core
- **NORMALIZED:** Sukumar delivers AI routing endpoint (phase 1) ready for integration with core, by Monday.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 35. `a16064fe`

- **ORIGINAL:** Set up HR intake decision call to discuss A2A vs. core routing approach
- **NORMALIZED:** Sukumar schedules HR intake decision call to discuss A2A vs. core routing approach, by Monday.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 36. `174f34c0`

- **ORIGINAL:** Provide estimate for LLM token quota and capacity planning for 1500+ concurrent users
- **NORMALIZED:** Vijay provides estimate for LLM token quota and capacity planning for 1500+ concurrent users, by today.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 37. `49f22cb3`

- **ORIGINAL:** Ping Jaffer via ABM email to ask if he has questions on the details email sent, to speed up form implementation work.
- **NORMALIZED:** Pallavi contacts Jaffer via ABM email to ask if he has questions on the details email sent, to speed up form implementation work, by today.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 38. `bcdc0cab`

- **ORIGINAL:** Follow up with Superman on cross-platform configuration updates.
- **NORMALIZED:** Suresh follows up with Superman on cross-platform configuration updates, by today.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 39. `14dc090f`

- **ORIGINAL:** Test CTS hardware intake form submission flow and validate API behavior; test software request form as alternative lighter flow.
- **NORMALIZED:** Pradeep tests CTS hardware intake form submission flow and validates API behavior and tests software request form as alternative lighter flow, by 2026-08-17.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 40. `a81336ed`

- **ORIGINAL:** Update the amendment document with scope details and cost figures from Scott's CR analysis; prepare for sending to customer.
- **NORMALIZED:** Todd updates the amendment document with scope details and cost figures from Scott's CR analysis and prepares for sending to customer, by before customer meeting.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 41. `ac4f36c4`

- **ORIGINAL:** Complete CTS Asset Management development including user profile API integration, non-mandatory field skipping, and hidden fees handling
- **NORMALIZED:** Vijay completes CTS Asset Management development including user profile API integration, non-mandatory field skipping, and hidden fees handling, by end of day Tuesday.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 42. `f5cf6c13`

- **ORIGINAL:** Pallavi to light up remaining CTS items for QA business validation
- **NORMALIZED:** Pallavi lights up remaining CTS items, for QA business validation, by Tuesday end of day.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 43. `178673c1`

- **ORIGINAL:** Deploy cross-platform access control solution to production after consulting with Sukumar
- **NORMALIZED:** Srikanth deploys cross-platform access control solution to production, after consulting with Sukumar.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 44. `5ed53b44`

- **ORIGINAL:** Discuss hardware form implementation approach with Vijay to determine if code changes in MCP are needed
- **NORMALIZED:** Pallavi Kandanur discusses hardware form implementation approach with Vijay, to determine if code changes in MCP are needed.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 45. `70cc84ce`

- **ORIGINAL:** Develop and present a plan for removing users from lower environments (QA/UAT) once they are promoted to production.
- **NORMALIZED:** Vijay develops and presents a plan for removing users from lower environments (QA/UAT) once they are promoted to production, by Monday.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 46. `b6675e85`

- **ORIGINAL:** Share deployed code repository archive and branch details to support debugging of QA integration issues
- **NORMALIZED:** Sukumar shares deployed code repository archive and branch details, to support debugging of QA integration issues, by end of day today.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 47. `c8c953ce`

- **ORIGINAL:** Complete cross-platform environment setup (BX to QA to prod).
- **NORMALIZED:** Sukumar completes cross-platform environment setup (BX to QA to prod), by Thursday.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 48. `75c63703`

- **ORIGINAL:** Follow up with support team and keep Pallavi updated on critical support ticket status
- **NORMALIZED:** Scott follows up with support team and keeps Pallavi updated on critical support ticket status, by today.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 49. `a99a01e4`

- **ORIGINAL:** Request extra privileges for current ServiceNow client credentials to access variable set definitions; coordinate with Tyler on account setup.
- **NORMALIZED:** Pradeep requests extra privileges for current ServiceNow client credentials to access variable set definitions and coordinates with Tyler on account setup, by 2026-08-17.

**Faithful?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

## Tally (fill in after marking)

- faithful: ___ / 49
- not faithful: ___ / 49
- unsure: ___ / 49
