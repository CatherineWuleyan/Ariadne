<div align="center">

# Ariadne

Research questions, connected to the evidence.

<a href="README.zh-CN.md"><img src="docs/images/language-en.svg" alt="English · Switch to 简体中文" width="208" height="38"></a>

</div>

Ariadne searches the Bohrium LKM paper corpus, extracts claims, and connects them across papers in an interactive graph. Start with a question; inspect the candidate answers, their premises, and the gaps in the evidence.

![Aircraft simulation design graph](docs/images/aircraft-question-graph.en.png)

[Get started](#get-started) · [Examples](#examples) · [Configuration](#configuration) · [How it works](#how-it-works)

## Get started

You need Python 3.11 or later. Live questions also require a Bohrium account and an OpenAI-compatible model endpoint. The offline demo requires neither account nor API key.

```bash
git clone https://github.com/CatherineWuleyan/ariadne.git
cd ariadne
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

## Examples

### Aircraft simulation design

**How should aircraft simulation design be carried out?**

The bundled aircraft graph covers 13 papers, 311 claims, and 258 relations. Five candidate answers organize the evidence around conceptual design, multidisciplinary optimization, flight dynamics, model correction, and hardware-in-the-loop validation. The frozen export contains 107 multi-step paths to these answers.

The red nodes are candidate answers. Green links support an answer; dashed links show other relation types. Select a node to inspect its claim and source, or select an answer to follow its premises.


### Pair breaking in 4Hₐ-NbSe₂

**Which pair-breaking channel sets the in-plane B_c2 in 4H_a-NbSe2?**

![Pair-breaking evidence graph](docs/images/physics-question-graph.en.png)

This reviewed example contains 18 papers and 500 claims. The full graph has 654 nodes and 1,298 edges; the opening view focuses on the explicit answer layer.

- **R1 — Ising protection and orbital limitation.** Ising spin–orbit coupling suppresses in-plane Zeeman pair breaking, while orbital and interlayer effects set the main field scale in the current interpretation.
- **R2 — Conditional orbital sufficiency.** A purely orbital explanation can reach the observed field if the anisotropy is large enough, approximately γ ≥ 5.6.

These answers are compatible. The unresolved issue is whether Ising protection is required to explain the measured scale. Cross-material analogies remain labeled as analogies; they are not direct evidence for this compound.


The aircraft demo rebuilds the graph and reasoning paths from the bundled inputs. The physics image illustrates a previously reviewed case; it is not a fresh live run.

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

A graph path is not a proof. Only deduction relations are eligible to form deductive chains. Inference, analogy, bounds, and same-paper reading sequences retain their own types. Candidate answers describe the current evidence; their presence in the graph does not establish that they are true.

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
