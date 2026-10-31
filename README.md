# LineSwitch

Follows the production schedule and keeps each line's cost code group in TimeClock Plus holding the cost codes of the work order that should be running.

How it maps to TCP+ (kiosk based, works with Multiple Schedule Organizations on):
* Work order = cost codes whose Level 1 is the WO number, for example 112\SETUP and 112\FILLING.
* Line = a cost code group named like "Line 1". It is attached to a fixed job code (LINE 1).
* Kiosk = a clock configuration (Line 1) with job code restrictions on, filtered to LINE 1 only. So the Line 1 kiosk only ever offers job code LINE 1 and the cost codes in group "Line 1".
* Every employee has LINE 1, LINE 2 and LINE 3, so anyone can clock in at any kiosk.
* Switching Line 1 from WO 112 to 113 = one call, PUT /v1/costcodegroups/{id} with removeCostCodes (112's) and addCostCodes (113's). Nothing is deleted, so old punches and reports stay intact, and reports by cost code show every WO.

Schedule file (Excel or CSV): Line, Work Order, Start, End. Optional: Date (if Start and End are only times), Cost Codes (comma list to override), Note. The Line value must match the cost code group name in TCP.

The board (open the Render URL). Built in React; index.html is the compiled page, the source is in LineSwitch_UI_source.zip:
* Board: one band per line in its own floor tape color. Big WO number, the steps workers can pick on that kiosk right now, progress through the WO, and the next WO with a countdown and its steps.
* Changeover alert: shows 15 minutes before a line switches, reminding workers to tap Change Cost Code after the switch.
* Pages: Board, Schedule (upload, template, export, timeline), Issues (fix or map WOs and lines), Activity, Setup (readiness checklist, TCP settings to keep, groups, API log), How it works.
* Wall view: big screen mode for a TV on the plant floor. Guide: explains how it works, right in the app.
* Export current schedule: GET /api/schedule.csv.

Preview mode is on by default: it shows what would change and changes nothing. Switch to Live in the top bar.

Environment: TCP_API_KEY, TCP_CLIENT_ID, TCP_CLIENT_SECRET, TCP_COMPANY_ID (220 for the tcpsales sandbox), BOARD_USER, BOARD_PASSWORD.
DATABASE_URL: the Neon connection string. With it, the schedule, mappings, holds and activity live in Neon and survive redeploys,
and every schedule change is kept in history (Schedule page, Schedule history) so you can go back. Tables are created on first start.

Schedule page: one column per line, listed like a schedule (7 to 3 PM WO 112, 3 to 5 PM WO 123), with day tabs.
Add, edit, remove, or move a work order earlier or later (the time slots stay, the work orders trade places),
then Save the line. Overlapping times are refused with a clear message. Saved changes apply right away.
Optional TCP_WO_SOURCE: level1 (default, as above) or jobcode (old model: WO = job code, its cost codes come from the groups attached to that job code).

Without DATABASE_URL the state falls back to state.json, which Render wipes on each redeploy.

## Floor features (new)

**Line tablet** (`#tablet`): put it on a tablet at each line. Pick the line once and type your name.
* Start a work order: shows that line's work orders for today from the schedule.
* Line down / Line back up: records downtime with a reason code.
* End work order / End shift: clocks everyone on that line off in TCP.

**Work orders** (`#orders`): every run with labor hours by step, labor cost, units planned, accepted and used, cost per unit and downtime. Also a By SO-SKU rollup, a Downtime log and a CSV export.

**Setup, How the floor works**:
* Setup starts each work order: lines wait for the setup person instead of switching on schedule times.
* Move people when a line switches: clocked in workers move to the same step on the new WO.
* Create missing cost codes in TCP: builds WO\STEP (and WO\FILLING\POSITION) from the step list.
* Labor rate, steps left out of unit cost, downtime reasons.

**Schedule file columns**: Line, Work Order, Start, End, SO-SKU, SKU, SKU Description, Production Units, Workers Assigned, Note.

**Areas**: any cost code group named Line N, Pre-Weigh, Compounding or Area N is treated as a line.
