# Feedback

- [x] **2026-04-29 18:36 — Bug: Erroneous duplication of order**
  Both tickets from MARK-10 Corporation are actually for the same order; the more recent one was confirmation of the shipping of the original one ordered on April 10th. Unclear why those were identified as different
  _tool: procurement-tracker · source: http://localhost:47821/_
- [x] **2026-04-30 13:23 — Bug: Records kept as separate rather than together**
  Both miniDSP records are for the same order, system did not realize this. Need to update for better comparison to know what order is what
  _tool: procurement-tracker · source: http://localhost:47821/_
- [x] **2026-04-30 13:31 — Feature: Add icon**
  Use icon.png image in the root folder as the favicon
  _tool: procurement-tracker · source: Manual input_
- [x] **2026-05-01 15:12 — Bug: Clicking pop-up notifications opens Apps Script**
  Clicking the push notifications still opens Apps Scripts instead of bringing me to the Mailroom GUI
  _tool: procurement-tracker · source: http://localhost:47821/_
- [x] **2026-05-06 15:07 — Feature: Received status**
  Final status should be "Received" not "Delivered"
  _tool: procurement-tracker · source: http://localhost:47821/_
- [x] **2026-05-06 15:08 — Feature: Received check-box**
  Every line should have a "Received" box that can be see on the card view and show up for lines that are in the "Delivered" state so I can quickly see what has been delivered but not received and go over to the rack to pick up
  _tool: procurement-tracker · source: http://localhost:47821/_
- [x] **2026-05-06 15:09 — Bug: Lead time not reflected in ETA**
  Some orders have lead time estimates in the .eml and these are not being reflected in the dashboard, ex. 6-8wk in email should show up as an estimated date based on when the email was sent
  _tool: procurement-tracker · source: http://localhost:47821/_
- [x] **2026-05-06 17:00 — Bug: StepperOnline 300609 wrong ordered date**
  Not sure why the date says Sun Jul 5, I ordered it today. Dates should be pulled from the email headers
  _tool: procurement-tracker · source: http://localhost:47821/_
- [x] **2026-05-11 09:11 — Bug: Does not seem like this is running as a background application**
  Make sure that the dashboard displays whether the application is being updated in the background as well as the default update frequency as a preference that can be set
  _tool: procurement-tracker · source: http://localhost:47821/_
- [x] **2026-05-11 10:20 — Bug: Manual updates to the item cards do not save**
  Example: Adding 871354287114 as tracking number to StepperOnline order and then clicking "Save" or CMD + Enter does not update ticket
  _tool: procurement-tracker · source: http://localhost:47821/_
