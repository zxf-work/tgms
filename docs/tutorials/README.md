# TGMS tutorials

Four short, task-shaped walkthroughs. Every command and output shown in
them was actually run against a real TGMS store while writing the page —
nothing is hypothetical.

1. **[Bring your own temporal graph data](bring-your-own-data.md)** —
   the event JSONL format, ingesting a tiny example, your first query,
   correcting a mistake in the data, and querying the belief state before
   vs. after the correction.
2. **[Give TGMS to an agent](agent-setup.md)** — MCP server setup, exactly
   what an agent discovers when it connects (tool listing, schemas), a
   minimal tool call, and what a good trace looks like.
3. **[Audit an answer](audit-an-answer.md)** — reading a real trace end to
   end: what gets verified, what "complete" means, and — stated plainly —
   what TGMS does *not* guarantee about an answer.
4. **[Maintain derived results](maintain-derived-results.md)** — register
   a query result as an artifact, land a correction, see exactly which
   artifacts are threatened and why, refresh only those, and watch a
   dependent artifact follow — with the old generations kept for audit.

Read them in order if you're new to TGMS; each stands alone if you already
have a store and just want one piece.

See also: [`docs/STABILITY.md`](../STABILITY.md) (what's durable across
upgrades), [`docs/PUBLIC_ROADMAP.md`](../PUBLIC_ROADMAP.md) (what's next),
and [the paper](../../paper/main.pdf) (the formal model behind tutorial 3,
for readers who want full precision).
