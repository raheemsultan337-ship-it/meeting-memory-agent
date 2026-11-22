# Evaluation on our own questions

**8/9 correct, 1 partial, 0 wrong** (LLM judge vs hand-checked gold; verdicts reviewed by hand below). Citations valid and from 2+ meetings: 9/9. Average required-fact coverage: 94%.

| id | kind | verdict | must | citations ok | steps |
|---|---|---|---|---|---|
| E01 | decision changed twice | correct | 1.00 | True | 4 |
| E02 | feature dropped + replacement | correct | 1.00 | True | 4 |
| E03 | reversal in final meeting | partial | 1.00 | True | 4 |
| E04 | who said what | correct | 1.00 | True | 3 |
| E05 | proposal -> doubt -> outcome | correct | 1.00 | True | 5 |
| E06 | numbers | correct | 1.00 | True | 2 |
| E07 | numbers | correct | 1.00 | True | 5 |
| E08 | objection + later trade-off | correct | 1.00 | True | 9 |
| E09 | hard: messy spoken numbers | correct | 0.50 | True | 7 |
| E10 | hard: implicit reference | not run | 0.00 | False | 0 |


## Human check of the judge's verdicts (done by reading the transcripts)

| id | judge | human | note |
|---|---|---|---|
| E01, E02, E04-E08 | correct | correct | checked against the evidence turns in my_questions.json |
| E03 | partial | **partial** | Says Speaker B proposed removing the LCD. In ES2002d#149 **Speaker A** proposed it; B (project manager) only summarised it ("what's our reviewed suggestion? Um take away the L_C_ display?"). |
| E09 | correct | **correct** | 12.35 (C), then 12.25 (B), vs the 12.50 budget. The `must` score is 0.5 only because the regex expected "three five" and the agent wrote "thirty-five". |
| E10 | not run | **not run** | Every free-tier text model was out of daily quota (flash-lite 500/day, 3.6/3.5-flash 20/day), and 3.8-flash returned 8 straight 503s. Re-run after the quota reset. |

**Final: 8/9 correct, 1 partial, 1 not run (quota).** All answered questions had valid citations from 2+ meetings.

## Per question

### E01 (ES2004): Team ES2004's energy source changed across their meetings. What did they first choose, what did they settle on in their third meeting, and what did the final design use?

- **Gold (hand-checked):** First rechargeable batteries with a docking station (ES2004b), then kinetic charging like in wrist watches (ES2004c), and the final design went back to standard batteries (ES2004d) to save cost.
- **Agent:** Team ES2004 first chose rechargeable batteries with a docking station, then in their third meeting they settled on kinetic charging with watch-type batteries, and the final design used standard batteries.
- **Citations:** ES2004b: "Yes. Rechargeable with a docking station."; ES2004c: "Mm, so can I just recap uh Sarah, for the decisions that we've made, kinetic charging, the watch-type batteries, um L_C_D_ display on th the top side of the flip"; ES2004d: "If we moved away from our much loved idea of a kinetic battery and just went with the standard batteries"
- **Judge:** correct: The answer accurately captures all three energy source changes: rechargeable batteries with a docking station initially, kinetic charging in the third meeting, and standard batteries for the final design.
- **Tool trace:** search -> search -> search -> submit_answer

### E02 (ES2003): Team ES2003 planned a way to find a lost remote. What was it, who was to look into it, what happened to it, and what did they rely on instead to make the remote harder to lose?

- **Gold (hand-checked):** A speaker and LED (beeping) locator; Speaker D (industrial designer) was to look at the speaker and LED; it was dropped in the final meeting because of budget cuts, and they relied on a special coloured case to make it harder to lose.
- **Agent:** Team ES2003 planned a remote locator using a transmitter attached to the television, a microphone/speaker on the unit, and a bright LED, with Speaker D assigned to look into it; however, the feature was eventually dropped due to budget constraints, and instead they relied on a special colored (yellow) case to make the remote harder to lose.
- **Citations:** ES2003b: "So I should just look at um the speaker, the speaker and an L_E_D_. And"; ES2003d: "So it looks like we're gonna get rid of the whole loca locator thing."; ES2003d: "let's say that and then we can have our special coloured case and then we at least have make it a little harder to lose."
- **Judge:** correct: The answer accurately captures all elements of the gold answer: the remote locator mechanism (speaker/LED), the designated speaker (Speaker D), the reason it was dropped (budget), and the alternative solution used instead (colored case).
- **Tool trace:** search -> search -> search -> submit_answer

### E03 (ES2002): Did Team ES2002's final design keep an LCD panel? What was said about it in meeting c, and what happened to it in meeting d?

- **Gold (hand-checked):** In ES2002c Speaker A (industrial designer) questioned what an LCD display would achieve; in ES2002d a basic LCD had been added but Speaker A suggested removing the LCD panel together with the advanced chip to save cost, so the LCD was taken out.
- **Agent:** No, Team ES2002's final design did not keep an LCD panel. In meeting c, Speaker B (project manager) stated there was no need for an LCD display or talk-back, and in meeting d, Speaker B suggested removing the LCD display entirely along with the advanced chip to stay within budget.
- **Citations:** ES2002c: "I would say no need for a talk-back. Uh does anybody disagree with that?"; ES2002d: "So w what's our reviewed suggestion? Um take away the L_C_ display?"
- **Judge:** partial: The answer correctly identifies that the final design did not keep an LCD panel, but it incorrectly attributes the quotes and suggestions to Speaker B (project manager) instead of Speaker A (industrial designer) as stated in the gold answer.
- **Tool trace:** search -> search -> search -> submit_answer

### E04 (ES2002): Which age group did Team ES2002 target, what shape did they adopt for the remote, and who suggested that shape?

- **Gold (hand-checked):** Fifteen to thirty-five year olds (ES2002b); a snowman shape, suggested by Speaker C (user interface designer) in ES2002c.
- **Agent:** Team ES2002 targeted the fifteen to thirty five age bracket and adopted a snowman shape for the remote control, which was suggested by Speaker C (user interface designer).
- **Citations:** ES2002b: "Okay, so fifteen to thirty five, look fairly young. You know, they have bit of expendable income to spend on this sort of thing."; ES2002c: "I'd quite like a sort of uh snowman type shape."
- **Judge:** correct: The answer correctly identifies the target age group, the snowman shape of the remote, and who suggested it, matching all details of the gold answer.
- **Tool trace:** search -> search -> submit_answer

### E05 (ES2005): Team ES2005 discussed fruit-shaped casings. Who suggested a banana, who doubted it, and what prototypes were shown in the final meeting?

- **Gold (hand-checked):** Speaker A (industrial designer) suggested a banana-shaped remote in ES2005c; Speaker D (user interface designer) was not sure about the banana idea; in ES2005d prototypes based on a banana and a tomato were shown.
- **Agent:** Speaker A (industrial designer) suggested a banana-shaped remote control, while Speaker D (user interface designer) initially doubted its practicality for pointing. In the final meeting, prototypes based on a banana and a tomato were shown.
- **Citations:** ES2005c: "Yeah? Okay, like we could have a big banana shaped remote control, 'cause it's yellow fruit, right?"; ES2005c: "Yeah. y I'm I'm not sure about the banana idea."; ES2005d: "Um one's based on the banana, one's based on the tomato and the other one is st"
- **Judge:** correct: The answer correctly identifies Speaker A as suggesting the banana, Speaker D as doubting it, and both banana and tomato prototypes in the final meeting, matching the gold answer.
- **Tool trace:** search -> search -> submit_answer -> submit_answer -> submit_answer

### E06 (ES2005): What production cost was Team ES2005 aiming for, and what unit cost did they report in their final meeting?

- **Gold (hand-checked):** They aimed for twelve Euros fifty (ES2005a) but the unit, minus the extra scroll buttons, came to fourteen point six Euros (ES2005d).
- **Agent:** Team ES2005 aimed for a unit production cost of twelve Euros fifty, but reported in their final meeting that the current unit production cost was fourteen point six Euros, as stated by Speaker C (project manager).
- **Citations:** ES2005a: "remember we're trying to make it for twelve Euros fifty."; ES2005d: "it's gonna cost us fourteen point six Euros."
- **Judge:** correct: The answer accurately states both the target production cost of twelve Euros fifty and the final reported cost of fourteen point six Euros, matching the gold answer.
- **Tool trace:** search -> submit_answer

### E07 (TS3009): What production cost limit did Team TS3009 start with, and what figure did their project manager give at the end as the lowest cost they could make a trendy remote for?

- **Gold (hand-checked):** Twelve and a half Euros (TS3009a); at the end Speaker A (project manager) said fourteen point one Euros (TS3009d).
- **Agent:** Team TS3009 started with a production cost limit of twelve and a half Euros, and Speaker A (project manager) later stated that fourteen point one was the lowest price they could make a trendy remote for.
- **Citations:** TS3009a: "we want to keep it our costs at twelve and a half Euros so, keep uh that in mind"; TS3009d: "I'll put in the report we that we think that fourteen point one is the l m lowest price you can make a remote for that's trendy d these days."
- **Judge:** correct: The answer correctly identifies the starting production cost limit of 12.5 euros and the final lowest cost stated by the project manager (Speaker A) as 14.1 euros, matching the gold answer.
- **Tool trace:** search -> search -> search -> search -> submit_answer

### E08 (TS3009): Team TS3009's project manager objected to an LCD screen early on. What was the objection, and in the final meeting who argued for keeping the LCD rather than the titanium?

- **Gold (hand-checked):** In TS3009b Speaker A (project manager) said that with twelve Euro fifty production cost they couldn't afford an LCD; in TS3009d Speaker D (marketing) argued to change the titanium to titanium-looking hard plastic rather than lose the LCD screen.
- **Agent:** Speaker A (project manager) objected to the LCD screen because it was unaffordable within the twelve Euro fifty production budget. In the final meeting, Speaker D (marketing) argued for keeping the LCD screen rather than the titanium.
- **Citations:** TS3009b: "Well with twelve Euro fifty as production cost, we can't uh afford an L_C_D_ uh"; TS3009d: "Or you shou It's Or change the titanium or dump the L_C_D_ screen. But I think you could better change uh titanium to hard a hard kind of plastic looking like titanium than lose the L_C_D_ screen."
- **Judge:** correct: The answer correctly identifies Speaker A's objection regarding the cost of the LCD screen and Speaker D as the person who argued for keeping the LCD instead of the titanium.
- **Tool trace:** search -> search -> search -> search -> search -> search -> find_facts -> read_turns -> submit_answer

### E09 (ES2004): In Team ES2004's final meeting, what production cost did they reach after removing the sample speaker, and how did that compare with their budget from the first meeting?

- **Gold (hand-checked):** Removing the sample speaker brought it to about twelve point three five Euros (Speaker C, ES2004d), just under the twelve point fifty Euro production cost set in ES2004a. (The meeting is confused: B also says twelve point two five.)
- **Agent:** In Team ES2004's final meeting, Speaker C (industrial designer) calculated that removing the sample speaker brought the production cost down to twelve point thirty-five Euros, before Speaker B (project manager) finalized it as twelve point twenty-five Euros. This was slightly below their budget from the first meeting, which Speaker B stated was a production cost of twelve fifty.
- **Citations:** ES2004d: "No 'cause it's samples sens sample speaker. If you took away that, that'll make it twelve point three five."; ES2004d: "It's two point five. No, it's twelve point two five."; ES2004a: "I think Yeah, um production cost's at twelve fifty, so"
- **Judge:** correct: The response correctly identifies both the production cost reached after removing the sample speaker (twelve point thirty-five / twelve point twenty-five Euros) and accurately compares it to the budget from the first meeting (twelve point fifty Euros).
- **Tool trace:** search -> search -> search -> find_facts -> read_turns -> read_turns -> submit_answer

### E10 (ES2002): Team ES2002 talked about using rubber in meeting c. What did they want rubber for, and what happened to the rubber in their final design?

- **Gold (hand-checked):** In ES2002c they planned plastic with rubber (rubber buttons / rubber on the case, like shock-resistant phones); in ES2002d they had to skip the rubber for the buttons and use plastic because of cost.
- **Agent:** (not run: daily API quota exhausted / model unavailable)
- **Citations:** 
- **Judge:** not run: gemini-3.8-flash: still failing after 8 attempts (agent:E10:step1)
- **Tool trace:** 

## Measured usage (all real API calls so far, from usage.jsonl)

| stage | calls | input tok | output tok | model time (s) |
|---|---|---|---|---|
| agent | 185 | 1,594,124 | 11,938 | 748 |
| brief | 2 | 4,244 | 14,725 | 121 |
| extract | 138 | 1,234,425 | 179,653 | 964 |
| index | 108 | 1,343,481 | 0 | 177 |
| judge | 9 | 1,940 | 700 | 82 |
| ping | 4 | 22 | 168 | 109 |
| search | 112 | 1,149 | 0 | 53 |
| timeline | 39 | 72,246 | 69,845 | 354 |
