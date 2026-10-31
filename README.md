# LineSwitch

Follows the production schedule and keeps each line's cost code group in TimeClock Plus holding the cost codes of the work order that should be running.

How it maps to TCP+ (kiosk based, works with Multiple Schedule Organizations on):
* Work order = cost codes whose Level 1 is the WO number, for example 112\SETUP and 112\FILLING.
* Line = a cost code group named like "Line 1". It is attached to a fixed job code (LINE 1).
* Kiosk = a clock configuration (Line 1) with job code restrictions on, filtered to LINE 1 only. So the Line 1 kiosk only ever offers job code LINE 1 and the cost codes in group "Line 1".
* Every employee has LINE 1, LINE 2 and LINE 3, so anyone can clock in at any kiosk.
* Switching Line 1 from WO 112 to 113 = one call, PUT /v1/costcodegroups/{id} with removeCostCodes (112's) and addCostCodes (113's). Nothing is deleted, so old punches and reports stay intact, and reports by cost code show every WO.

Schedule file (Excel or CSV): Line, Work Order, Start, End. Optional: Date (if Start and End are only times), Cost Codes (comma list to override), Note. The Line value must match the cost code group name in TCP.

Preview mode is on by default: it shows what would change and changes nothing. Switch to Live in the top bar.

Environment: TCP_API_KEY, TCP_CLIENT_ID, TCP_CLIENT_SECRET, optional TCP_COMPANY_ID (default 1), BOARD_PASSWORD.
Optional TCP_WO_SOURCE: level1 (default, as above) or jobcode (old model: WO = job code, its cost codes come from the groups attached to that job code).

State (schedule, holds, activity) is kept in state.json. On Render that file resets on each redeploy, so upload the schedule again after a deploy.
