# Judgement confidence: checking a link you cannot grade

Ariadne labels a claim `support`, `oppose` or `bounds` against a candidate with a
single model call. In development you can compare that call against a hand-graded
sample. In use — a different domain, a paper the model has never seen — you
cannot. This note is about what can still be checked **at run time, per link,
without gold labels**.

## Where the label comes from

| Layer | Producer | Values |
| --- | --- | --- |
| Reasoning edge | Step 3, one call per candidate pair | `deduction`, `infer`, `analogy`, `bounds`, `special_case`, `conjunction` |
| Answer link | Step 6, one call per candidate answer | `support`, `oppose`, `bounds` |
| Reading order | deterministic, by paper and claim number | `same-paper` |

Only the reasoning edge can form chains (`chainable = deduction`). The answer
link is what the verdict counts. So "this root is multi-paper supported" means:
one call per root returned at least N `support` links, and N-1 of them came from
a second paper.

That single call sees a 44-item candidate pool and a 300-character summary of each
claim. Nothing independently confirms the label afterwards. On the frozen
aircraft graph, 23 of 45 support links (51%) point at claims the candidate itself
nominated as seeds in the same generation call — self-confirmation that is
currently invisible in the output.

## What can be checked without gold

Three groups, ordered by cost. Each produces a **flag on the link**, not a new
verdict: the checks report, the caller decides.

### Structural (deterministic, free)

| Check | Failure mode it detects |
| --- | --- |
| `self_seeded` | the link supports a claim the candidate itself proposed as a seed |
| `single_paper` | all support for a root comes from one paper, so "multi-paper" is a label, not corroboration |
| `truncated_input` | the claim text was clipped at 300 characters, so the judge never saw the end of the sentence |
| `thin_claim` | the claim is too short to carry a condition or a result |
| `no_second_reader` | the link has never been seen by anything except the judge that wrote it |

### Lexical (deterministic, free)

| Check | Failure mode it detects |
| --- | --- |
| `paraphrase` | claim and candidate share most of their vocabulary, so the link may restate the candidate rather than evidence it |
| `topic_drift` | claim and candidate share almost no vocabulary, so the judge is extrapolating |
| `condition_mismatch` | the claim names a material, temperature, dataset or model that the candidate does not, so support may not transfer |
| `no_graph_neighbourhood` | the linked claim has no shared-term neighbour in the graph, so this support is an isolated assertion |

### Perturbation (billable, sampled)

| Check | Failure mode it detects |
| --- | --- |
| `negation_flip` | asking about the negated candidate still yields support, so the judge is matching topics, not entailment |
| `paraphrase_flip` | an equivalent candidate loses the support, so the label is wording-sensitive |
| `context_flip` | giving the claim's surrounding claims instead of its 300-character summary flips the label, so the input was insufficient |
| `model_disagreement` | a second model does not reproduce the label |

Perturbations are the only checks that can detect a confident-but-wrong judge,
and they are the only ones that cost money. Run them on a sample, or on the
links that decide a verdict, not on everything.

## Domain shift

You cannot ask a model whether it knows the domain. You can ask whether the
evidence covers the vocabulary the question uses — a proxy that is computable and
domain-agnostic:

- **candidate coverage**: fraction of the candidate's content terms that appear
  anywhere in the graph. Low coverage means the judge is extrapolating.
- **neighbourhood density**: how many claims share at least two content terms
  with the candidate. One or two means the route is thin regardless of labels.
- **question coverage**: fraction of the question's content terms present in the
  graph. Low coverage is a graph-level warning: the corpus may not be about what
  was asked, and every downstream label inherits that.

These are run-level facts, reported once, not per link.

## Tiers, and the honesty rule

Combine the flags into three tiers so a reader sees at a glance what happened:

| Tier | Conditions |
| --- | --- |
| `corroborated` | support from ≥2 papers, second paper ≥2 claims, no `self_seeded`-only support, no `truncated_input`, no drift flag |
| `single_pass` | support exists, but one or more flags apply |
| `disputed` | a perturbation check contradicts the label, or two models disagree |

And the rule the rest of the product already uses: **state what was not run**.
A link with no perturbation checks is not a confirmed link; it is an unchecked
one. The report carries `checks_run` and `checks_skipped`, and the tier is
qualified by which group ran.

## Where it lands

- `ariadne/verify.py`: `verify(answer_layer, view, *, controls=False, sample=0)`
  returns a verification overlay. It never edits the answer layer.
- `ariadne verify --answer ... --view ... [--controls] [--sample N]` writes
  `verification.json` next to the answer layer.
- `ariadne support` runs the structural and lexical groups inline and prints the
  tier; `--controls` adds the perturbations for that candidate.
- The viewer shows the tier on the root and the flags on each link. A `disputed`
  root keeps its colour but gains a badge — the graph still shows what the judge
  said, and shows that something checked it.
- `run.json` records which groups ran, so a run cannot silently claim a tier it
  never earned.

## Checking the checks

The checks are heuristics, so they need the same treatment as the judge: on a
hand-graded sample of 40 links, measure

- **flag precision**: when `paraphrase` fires, is the link actually wrong?
- **tier monotonicity**: does human agreement fall from `corroborated` to
  `single_pass` to `disputed`?

If a flag does not predict human disagreement, it is noise with a name, and it
should be removed rather than shipped.
