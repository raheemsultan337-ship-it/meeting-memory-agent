# 3-5 minute walkthrough script

Open a terminal in `meeting-memory`, run `.venv\Scripts\activate`, and have VS Code open beside it.

## 0:00 The problem and the shape of the answer (30s)
"138 real meetings, about 676k words. The brief says to treat them as a store you retrieve from, not something you paste into a prompt, and to assume 10,000 meetings. So no prompt ever holds more than one meeting. Memory has three layers: the raw turns with keyword and vector search, extracted facts with verified quotes, and a per-team timeline of what changed."

## 0:30 Memory over time: how it knows what is current (60s)
```
python timeline.py --show TS3009
```
Point at **case material [CHANGED]**: hard plastic (a), then titanium back (c), then titanium-coloured plastic (d). Every step has the speaker and an exact quote. Point at the OPEN line: Speaker A's unresolved objection.
"The LLM judges that c replaces a; code puts the meetings in order and checks every fact id. Order is a fact, not a judgement."

## 1:30 The agent, live (60s)
```
python agent.py ask "Team TS3009 planned a titanium back for their remote. Who first proposed replacing the titanium, what did they propose instead, and was the titanium kept in the end?"
```
Narrate the printed steps: it searches, reads turns, submits; if the gate rejects a quote it gets the reason and fixes it. "The answer is Speaker B, titanium-coloured plastic, and the titanium was dropped, with citations from two meetings."

## 2:30 Why hybrid search (30s)
```
python search.py --compare "how did they stop the remote getting lost?" ES2005
```
"Keyword search finds 'did you get my email' because of the words 'did' and 'get'. Vectors find the whistle passage. RRF fuses the two rankings."

## 3:00 Model vs code, and honesty (60s)
Open `WRITEUP.md`, show the model-vs-code table. Then `eval/report.md`: the score on my own hand-checked questions, and the three it got wrong and why.
"The citation gate is code because 'is this string inside that turn' is a string check, and LLMs paraphrase. No quote reaches answers.json without passing it."

## 4:00 Where it broke, and scale (45s)
NOTES.md: the free-tier embedding limit of 1,000 texts a day forced 1,000-word chunks; `L_C_D_` broke keyword search; a model was retired mid-build. "Every call is cached and every stage resumes, so a quota stop loses nothing. At 10,000 meetings: pgvector, a worker queue, paid quota, grouping by participants instead of team labels."

## If they give you new questions live
```
python agent.py ask "Team <ID> ... <their question>"
```
The question must contain the team id. Each new question uses about 6 flash-lite calls and about 3 embedding requests from the daily quota.
