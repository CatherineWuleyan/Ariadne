# pipeline_merge_question

`pipeline_merge_question` 是 `pipeline_merge` 的**副本**，用于在领域图最后增加一层
“问题 → 候选回答根节点 → 证据链”。原 `pipeline_merge` 未修改。

## 1. 做了什么

原 merge pipeline 以 Step 0–5 产出 claim-level 领域图。它没有回答层；上次飞机
数据集还显式设置了 `summarize_conclusions: false`，所以连 K（领域结论）层也没有。
本副本增加：

| 文件 | 作用 |
| --- | --- |
| `question_layer.py` | 回答层核心：根节点生成、claim 连接、typed 链预计算、视图注入、评估 |
| `step6_question_layer.py` | Pipeline Step 6：把回答层固化为 `question.answer_layer` artifact |
| `question_view.py` | View adapter：把回答层注入 Step 4/5 的领域图，输出 316 节点/273 边的 typed view |
| `run_question_layer.py` | 独立 runner：只对冻结的 `domain_view_model.json` 跑最后的回答层 |
| `edge_role_audit.py` | 对已有图做 edge-role 审计；`--rules-only` 为确定性分类，LLM 模式可细化 |
| `build_typed_view.py` | 对冻结图补回 Step 4 不能编译的 `special_case/reproduces/bounds` display links |
| `export_chains_markdown.py` | 导出所有指向 R 的多步链（same-paper + graph path）到 Markdown/Mermaid |
| `viewer_question.html` | 问题主导 viewer 模板（根节点、回答边、reasoning type、exploratory 开关、根节点详情） |
| `render_question_viewer.py` | 把任意 view model 注入 viewer 模板 |
| `pipeline.question.json` | 完整 pipeline 配置：13 篇飞机论文 + K 层 + Step 6 + 新 view adapter |

数据流：

```text
Step 0-5 (已有 merge pipeline)
        ↓  compiled claim graph / domain_view_model.json
Step 6  问题 + 图摘要
        ├─ LLM 生成 3-5 个候选回答（root）
        ├─ 每个 root 在冻结候选池里判断 support / bounds / oppose
        └─ 预计算 root 的 premise-to-answer 链
        ↓  question.answer_layer artifact
QuestionViewAdapter
        ↓  roots + answer edges + metadata.answer_layer
viewer_question.html
```

## 2. 候选回答是怎么生成的

`question_layer.py` 不创造新领域事实，只做三件事：

1. **根节点生成**：把图压缩成 72 条代表 claim 的 digest（按度数、每篇论文代表、
   integration delta），让 LLM 针对问题提出 3–5 条“HOW”层面的候选路线，并给出
   `seed_claim_ids`、`criteria`、`scope`。如果图中已有 K 结论，prompt 会要求优先
   以 K 作为回答的直接证据。
2. **claim 连接**：每个 root 从 seed、1 跳邻居、词面 top、每篇论文代表、delta
   节点组成 44–60 条候选池；LLM 只能在这个冻结池里选 `support` / `bounds` /
   `oppose`，机械校验 ID、去重、拒绝同 claim 的矛盾关系。
3. **typed 链预计算**：从 root 的 support claim 沿 **chainable** 边向上枚举到叶子。
   只有 `deduction` 是 chainable；`infer` 是 residual，`special_case / reproduces /
   bounds / abduction / analogy` 都不进入 typed support chain。旧的全 `infer`
   路径保留为 `exploratory_paths`，明确标注 not evidence；`conjunction` 仍不走进链。

根节点的 verdict 只是结构描述，不是真值：

| verdict_class | 条件 |
| --- | --- |
| `contested` | 至少一条 oppose |
| `primary` | support ≥ 3 且跨 ≥ 2 篇论文 |
| `partial` | 有 support，但未达到 multi-paper |
| `exploratory` | 没有任何 support（该 root 通常会被丢弃） |

## 3. 运行方式 A：对当前冻结图只跑回答层（本次使用）

```bash
cd <repo>

# 1) 对已有图做 edge-role 分类；--rules-only 不调用 LLM
python3 pipelines/merge/pipeline_merge_question/edge_role_audit.py \
  --rules-only \
  --out outputs/aircraft/final/edge_roles.json

# 2) 补回 Step 4 丢弃的 typed display links（本次 15 条）
python3 pipelines/merge/pipeline_merge_question/build_typed_view.py \
  --view outputs/aircraft/versions/03-reviewed-handcrafted-2026-10-14/source_snapshot/domain_view_model.json \
  --step3 outputs/aircraft/versions/02-native-311-2026-10-13/artifacts/blobs/artifact_42958528016f4cbe972f8675b327ee44.json \
  --roles outputs/aircraft/final/edge_roles.json \
  --out-view outputs/aircraft/final/domain_view_typed.json \
  --out-roles outputs/aircraft/final/edge_roles_typed.json

# 3) 跑回答层（fixture 可离线重放；真实 LLM 时去掉 --fixture）
python3 pipelines/merge/pipeline_merge_question/run_question_layer.py \
  --view outputs/aircraft/final/domain_view_typed.json \
  --edge-roles outputs/aircraft/final/edge_roles_typed.json \
  --fixture outputs/aircraft/final/answer_fixture.json \
  --out outputs/aircraft/final
```

默认问题：

- zh: `怎么完成飞机的仿真设计？`
- en: `How can aircraft simulation design be carried out?`

可用 `--question-zh` / `--question-en` / `--domain` / `--max-roots` 覆盖。
LLM key 从 `agent/.env` 读取（与 Step 3 相同的 `_post_json`），可通过
`GAIA_USAGE_LOG` 记录 token。

离线重放（不调用 LLM）：

```bash
python3 pipelines/merge/pipeline_merge_question/run_question_layer.py \
  --view outputs/aircraft/versions/03-reviewed-handcrafted-2026-10-14/source_snapshot/domain_view_model.json \
  --fixture outputs/aircraft/final/answer_fixture.json \
  --out outputs/aircraft/final_replay
```

输出：

```text
outputs/aircraft/final/
  answer_layer.json           # roots / answer edges / typed chains / exploratory paths
  domain_view_question.json   # 原 view + roots + answer edges + top-level answer_layer
  domain_viewer_question.html # 自包含 viewer
  domain_view_typed.json      # 补回 typed display links 的 base view
  edge_roles.json             # 规则/LLM edge-role 审计
  edge_roles_typed.json       # 含 15 条 display-only links 的角色表
  evaluation.json
  evaluation.md
  llm_raw.json                # 生成与连接两阶段 prompt/response 审计
  answer_fixture.json          # 根节点与连接结果，可离线重放
```

## 4. 运行方式 B：完整 pipeline（含 K 层）

`pipeline.question.json` 与旧飞机配置相同（13 篇论文），但：

- Step 3 设置 `summarize_conclusions: true`，恢复被跳过的领域结论层；
- 末尾新增 `step6_question_answer_layer`；
- `view_adapter` 改为 `pipeline_merge_question.question_view:QuestionViewAdapter`。

```bash
cd <repo>
export PYTHONPATH="$PWD/agent:$PWD/pipelines/single_paper:$PWD/pipelines/merge"

.venv-gaia-a7/bin/python -m pipeline_harness init \
  --pipeline pipelines/merge/pipeline.question.json \
  --inputs weekly_reports/manifests/merge_aircraft_domain_graph.json \
  --runs-root outputs/question_domain_runs

# 取 init 输出的最后一行 run_dir
.venv-gaia-a7/bin/python -m pipeline_harness run --run <run_dir>
.venv-gaia-a7/bin/python -m pipeline_harness view --run <run_dir>

python3 pipelines/merge/pipeline_merge_question/render_question_viewer.py \
  --view-model <run_dir>/views/view_model.json \
  --out <run_dir>/views/domain_viewer_question.html
```

注意：完整运行会重新执行 Step 2/3 的 LLM 判断，成本和耗时明显高于方式 A；
它也会重新生成 K 层，因此根节点的直接证据可能从 claim 变成 K。两种方式共用
同一个 Step 6 和同一个 viewer。本次只对配置做了 `pipeline_harness init` /
`check` 验证，实际执行的是方式 A；完整 Step 0–6 尚未跑。

## 5. 本次结果（冻结图，无 K 层，typed-edge revision）

- 基础图 311 claims / **223 edges**（补回 15 条 `special_case`/`bounds` typed links）；
- 5 个候选回答，50 条 answer edge（45 support + 5 bounds，无 oppose）；
- 46 条 claim 被直接连接；**typed support chains = 0**；
- exploratory paths：R1 18 / R2 24 / R3 24 / R4 20 / R5 14；直接+exploratory
  覆盖 86 claims（connected 的 35.8%），但 exploratory 不计入 evidence；
- 边角色：`analogy 52 / infer 148 / conjunction 18 / bounds 4 / special_case 1`，deduction 0；
- 跨论文 101 条全部保留并默认显示：analogy 52 / infer 26 / conjunction 18 / bounds 4 / special_case 1；54 条经过人工审阅改类型，见 `CROSS_PAPER_AUDIT.md`；
- 8 篇论文被直接支持触及；
- 本轮回答层复用 fixture，未新增根/连接 LLM 调用；edge role 为规则分类。

详细评估见 `outputs/aircraft/final/PIPELINE_EVALUATION.md` 和
`evaluation.md`；逐根典型链与可靠性分级见
`outputs/aircraft/final/CHAIN_RELIABILITY.md`；全部多步链见
`outputs/aircraft/final/ALL_CHAINS_TO_ROOTS.md`。

## 6. 已知限制

1. 当前冻结图没有 K 层，回答直接连 claim；根节点的支持偏向单篇论文的
   dominant evidence，跨论文主要体现在链上，而不是每个根都有均衡的多论文直接支持。
2. 图中没有 contradiction 关系，所以没有 oppose；verdict 全部是 primary，
   这不等于“所有回答都成立”。
3. 连接阶段候选池有界（44–60 条）；池外的相关 claim 会被判 none，是召回上限。
4. 根节点由一次 LLM 调用生成；提示词/模型变化会改变根集合。生产使用应加人工
   accept/reject 或冻结 fixture。
5. viewer 是原型：默认 answer-focused 视图只显示 direct support + typed chains；
   `Exploratory paths` 默认不勾选，勾选后显示旧的 infer 路径（灰色虚线，不计 support）；
   `Full domain graph` 默认不勾选，勾选后显示全部 connected domain nodes（本次
   226 个），布局会更散。

## 7. 下一步建议

- 用完整 `pipeline.question.json` 跑一次含 K 的图，比较根节点直接连 K 与连
  claim 的支持质量；
- 增加人工审核 UI，把根节点/连接写入可复用 fixture，保证跨 run 的稳定身份；
- 加入 contradiction/opposition 来源，让 `contested` 真正可用；
- 对连接做第二轮扩大召回，并用人工标注集评估 precision/recall；
- 对 `edge_role_audit.py` 的 LLM 模式做小批量人工 gold set 校准；当前冻结图
  使用 `--rules-only`，其中 154 条 paper-internal 边仍是 residual `infer`，
  需要逐条回原文才能升级为 deduction/special_case。
- 给 viewer 增加“只看 direct support / 只看 typed chain / 显示 exploratory”的
  三档过滤，并保留当前的 root `no typed chain` badge。
