Before the tickets: when `.agent/review-answers.json` exists, read it. It
holds the operator's decisions at the plan gate (`answers`, by question id;
the questions are on `.agent/review.html`). Apply them over the tickets where
they disagree. Never commit it. An answer whose id maps to no question is
ignored and listed in your report. A file with `"submitted": null` is a draft,
not a decision.
