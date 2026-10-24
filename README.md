<div align="center">

# Ariadne

An evidence-to-decision tool for scientific research.

<a href="README.zh-CN.md"><img src="docs/images/language-en.svg" alt="English · Switch to 简体中文" width="208" height="38"></a>

</div>

Ariadne helps researchers compare explanations, trace them to paper evidence, and identify what still needs to be verified before making a research decision.

Start with a question. Ariadne retrieves papers, extracts their claims, and connects them to candidate answers in an interactive graph. Follow an answer back to its premises to see what supports it, where it depends on assumptions, and where the evidence runs out.

![Aircraft simulation design graph](docs/images/aircraft-question-graph.en.png)

[From evidence to a decision](#from-evidence-to-a-decision) · [Examples](#examples) · [Get started](#get-started) · [Configuration](#configuration)

## From evidence to a decision

Before choosing a direction, researchers need to check whether results transfer to their setting, which explanations the evidence supports, and what remains uncertain.

| Research question | What to inspect in Ariadne | Decision it helps inform |
| --- | --- | --- |
| Which explanation should I pursue? | Candidate answers and the claims connected to each | Which explanation warrants further investigation |
| Does this result apply to my problem? | Source claims, assumptions, and typed cross-paper relations | Whether the evidence transfers under the required conditions |
| What needs to be verified next? | Conditional support, conflicting claims, and missing premises | Which uncertainty to resolve before committing to a direction |

Ariadne provides the evidence graph and reasoning paths. The researcher uses them to make the decision.

## Examples

### Aircraft simulation design

**How should aircraft simulation design be carried out?**

This case organizes the evidence for choosing modeling and validation steps in an aircraft simulation workflow. The five candidate answers cover conceptual design, multidisciplinary optimization, flight dynamics, model correction, and hardware-in-the-loop validation.

The bundled inputs cover 13 papers, 311 claims, and 223 edges. The offline demo exports 107 paths to the candidate answers: 7 same-paper reading sequences and 100 `infer` exploratory paths. **It produces zero typed support chains** -- deduction is the only chainable relation type, and this graph has none. That is the honest state of the demo, not a rendering bug.

The candidate answers are labelled by how much of their support comes from a second paper: R1 and R2 are `multi-paper` (7+3 and 5+3 claims), while R3-R5 are `single-paper core` (7+1, 9+1, 8+1). Every root records `support_paper_counts`, so the split can be checked rather than inferred.

The red nodes are candidate answers. Green links support an answer; dashed links show other relation types. Select a node to inspect its claim and source, or select an answer to follow its premises.

### Pair breaking in 4Hₐ-NbSe₂

**Which pair-breaking channel sets the in-plane B_c2 in 4H_a-NbSe2?**

![Pair-breaking evidence graph](docs/images/physics-question-graph.en.png)

This case compares the evidence and assumptions behind two compatible explanations of an observed critical field. The reviewed graph brings together 18 papers and 500 claims; the image focuses on the answer layer.

- **R1 — Ising protection and orbital limitation.** Ising spin–orbit coupling suppresses in-plane Zeeman pair breaking, while orbital and interlayer effects set the main field scale in the current interpretation.
- **R2 — Conditional orbital sufficiency.** A purely orbital explanation can reach the observed field if the anisotropy is large enough, approximately γ ≥ 5.6.

The unresolved issue is whether Ising protection is necessary to explain the measured scale. Following the two answers exposes the assumptions that need independent verification before settling on a mechanism. Cross-material evidence remains labeled by its relation type, allowing the researcher to assess whether it applies to this compound.

The aircraft demo rebuilds the graph and reasoning paths from the bundled inputs. The physics image illustrates a previously reviewed case; it is not a fresh live run.

## Get started

You need Python 3.11 or later. Live questions also require a Bohrium account and an OpenAI-compatible model endpoint. The offline demo requires neither account nor API key.

```bash
git clone https://github.com/CatherineWuleyan/Ariadne.git
cd Ariadne
./install.sh
./run.sh demo
```

Open the HTML path printed by the demo in your browser. It includes the graph data and needs no server.

For a live question, install the Bohr CLI and configure your accounts:

```bash
npm install -g bohr-cli
./run.sh config init
./run.sh auth bohr
./run.sh doctor

./run.sh ask "How should aircraft simulation design be carried out?" \
  --top-k 2 --dry-run
./run.sh ask "How should aircraft simulation design be carried out?" \
  --top-k 2 --no-fulltext --yes
```

`--dry-run` checks the setup and prints the plan without paid calls. `--yes` authorizes billable LKM calls for that run. LKM retrieval, optional graph extraction, PDF parsing, and model calls can incur charges; check your providers' current rates. Start with two papers to inspect the result before increasing the scope.

## Configuration

```bash
./run.sh config init
./run.sh config show
./run.sh config test --api
./run.sh auth status
```

Configuration is stored in `~/.config/ariadne/config.json`. Use [the example](config.example.json) as a reference. Environment variables override the file; command-line flags override both.

| Setting | Purpose |
| --- | --- |
| `model.api_key` | Your model provider's key |
| `model.base_url` | OpenAI-compatible API base URL |
| `model.model` | Model for claim extraction and single-paper processing |
| `model.merge_model` | Model for graph merging and candidate answers |
| `bohr.profile` | Bohr profile; empty uses the active profile |
| `runtime.python` | Interpreter for pipeline subprocesses |
| `retrieval.max_extractions` | Limit on missing paper graphs to build |

For shell or CI use, set `DEEPSEEK_API_KEY` (or `OPENAI_API_KEY`) and `BOHR_ACCESS_KEY`. Set `ARIADNE_MODEL_BASE_URL`, `ARIADNE_MODEL`, and `ARIADNE_MERGE_MODEL` for a different compatible endpoint. DeepSeek is the tested default. Single-paper and merge processing require a chat-completions endpoint.

Useful commands:

```bash
./run.sh ask --help
./run.sh ask "Your research question" --papers papers.json --top-k 2
./run.sh ask "Your research question" --no-lkm-extract --top-k 2 --yes
```

`--papers` accepts local graph and text paths in place of live search. Entries with only an LKM ID still fetch their graph. `--no-lkm-extract` disables building missing graphs.

## How it works

```text
Question → LKM retrieval → Claim extraction → Single-paper graphs
         → Cross-paper merge → Candidate answers → HTML graph + reasoning paths
```

The repository includes the Ariadne CLI, the Gaia pipeline harness, and the three pipelines it invokes. Each run writes to a separate directory under `outputs/ariadne_runs/`.

| Output | Contents |
| --- | --- |
| `domain_viewer_question.html` | Interactive graph with embedded data |
| `ALL_CHAINS_TO_ROOTS.md` | Multi-step paths, with relation types and Mermaid diagrams |
| `run.json` | Run metadata, stage timing, and output locations |
| `01_retrieval/selection.json` | Selected papers and retrieval decisions, for live runs |
| `logs/` | Stage logs |

Relations retain their types: deduction, inference, analogy, bounds, and same-paper reading sequences have different roles. Only deduction relations are eligible to form deductive chains. Candidate answers and visible paths are starting points for examining the evidence; their presence alone does not establish a conclusion.

## Development

```bash
source .venv/bin/activate
PYTHONPATH=.:agent:pipelines/single_paper:pipelines/merge \
  python -m unittest discover -s ariadne/tests -p 'test_*.py'
```

| Directory | Contents |
| --- | --- |
| `ariadne/` | CLI, configuration, retrieval, orchestration, and tests |
| `agent/` | Pipeline harness, validation, and artifact storage |
| `pipelines/clean_claims/` | Paper-to-claims extraction |
| `pipelines/single_paper/` | Single-paper Gaia IR processing |
| `pipelines/merge/` | Cross-paper merging, answer layer, and rendering |
| `examples/aircraft/` | Inputs for the offline aircraft example |

Extracted from [Gaia IR](https://github.com/CatherineWuleyan/gaia-ir). The compiler dependency is pinned to `gaia-lang==0.5.0a7`. A license has not yet been specified.
