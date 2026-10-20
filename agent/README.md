# Pipeline Harness

`agent/` 是当前 Agent 的可执行骨架，由“领域无关的 Harness 内核”和“Pipeline 7.0 领域插件”两层组成。Harness 内核负责运行插件、冻结输入、登记产物、保存 checkpoint、恢复运行和投影视图；它不直接解释论文、命题、weakpoint、formalization 或 Gaia-IR。对这些对象的理解集中在 `pipeline_harness/domain/`、`real_inputs.py` 和相应插件中，因此领域流程可以演进，而不必把 Runner 改成一个包含所有业务分支的中央脚本。

## 当前包含的能力

- `Run`、`ArtifactRef`、`Checkpoint` 和 `Finding` 基础契约；
- manifest 外部输入冻结、配置快照 hash 和本地原子写入；
- 可继续执行的顺序 `StagePlugin` Runner；
- `waiting / failed` 审计产物隔离与 checkpoint fork；
- 仅用于契约测试的两阶段 synthetic pipeline；
- 由 adapter 驱动的 `ViewDocument` 投影；
- 支持搜索、图层开关、最终 `Overview / Standard` 双模式及开发者步骤视图的单文件 Viewer；
- 离线完整性检查和 `unittest` 测试。

Synthetic 数据不是 gold standard。默认 pipeline 不读取 `01_paper_sample_one_ticket/`，也不调用网络服务或 LLM；只有显式配置语义工具的 Stage 才会调用对应服务。

## 当前 Agent 架构

### 总体分层

当前实现可以看成六个协作层，而不是一个自治大模型循环：

```text
CLI / pipeline JSON
        │ 选择 pipeline、冻结配置
        ▼
Runner ─────────────── RunStore / Artifact Store / Checkpoint / Event Log
  │                         ▲
  │ StageContext            │ ArtifactDraft、Finding、StageResult
  ▼                         │
StagePlugin ────────────────┘
  │
  ├─ deterministic domain code
  │    导入、ID 对齐、revision 继承、规则校验、索引
  │
  ├─ DomainTool（语义审核用途）
  │    图片理解、claim 补齐、necessity test 等语义判断
  │
  └─ official compiler tool
       formalization → 官方 Gaia IR

Artifact Store ──ViewAdapter──▶ ViewDocument ──template──▶ viewer.html
```

各层职责如下：

|层|当前模块|职责与边界|
|---|---|---|
|入口与装配|`cli.py`、`domain/workflow.py`|选择 pipeline，读取声明式 stage 列表和插件参数；不实现 stage 业务|
|运行内核|`runner.py`、`plugins.py`|创建 `StageContext`、装载插件、执行 attempt、解释 `succeeded / waiting / failed`，成功后提交 checkpoint|
|持久化与审计|`store.py`、`models.py`|冻结输入和配置、内容哈希、原子写入、Artifact 索引、Run 状态、Finding 与事件日志|
|Pipeline 7.0 领域层|`domain/stages.py`、`runtime.py`、`authoring.py`|实现 Step 1–4，维护唯一 `formalization.json` revision 链，处理稳定 ID、来源锚点和领域对象历史|
|机械门禁|`domain/validation.py`、`provenance.py`、`indexing.py`、`compiler.py`|执行引用闭包、形状、来源定位和官方 Gaia 校验；确定性规则优先于模型判断|
|派生视图|`view/`、`domain/view.py`|把 Artifact 投影成 `ViewDocument`，生成搜索、图层和 revision 视图；不回写领域真源|

这种分层同时保留了两条不同的状态链：

- 运行状态链：`run.json → checkpoint → events.ndjson`，回答“执行到哪里、哪次 attempt 成功、失败原因是什么”；
- 领域真源链：冻结输入 → `input.bundle` → `formalization.json` revision → 最终索引 → 官方 Gaia IR，回答“当前认可的科学知识与推理结构是什么”。

二者通过 `ArtifactRef` 关联，但不会互相复制。Viewer、validation report 和 tool response 都是可重建或只供审计的派生产物，不能成为第二份 authoring truth。

### 一次 Stage 的执行生命周期

Runner 对所有 Stage 使用相同生命周期：

1. 校验冻结的 pipeline 配置 hash，拒绝运行中静默改配置；
2. 组装固定输入：`run.input_artifacts + last_successful_checkpoint.artifacts`；
3. 为本次 attempt 创建独立 `work/<stage>/attempt_*` 目录和只读输入视图；
4. 根据 `module:object` 字符串装载 `StagePlugin`，传入 `StageContext`；
5. 插件在 attempt 工作目录产生候选文件，并返回 `StageResult`；
6. Runner 统一复制文件、计算 SHA-256、登记 `ArtifactRef`、记录 Finding 和事件；
7. 只有 `succeeded` 会创建新 checkpoint 并推进 stage；`waiting` 产物只作审计，`failed` 产物只作诊断，不会成为下一 Stage 的输入。

这个提交模型接近一个很小的事务边界：插件可以失败或重试，但下游只能观察到最后一次成功提交。Runner 捕获未处理异常并转换为结构化 `PLUGIN_EXCEPTION` Finding，因此单个插件不需要自行实现整套恢复协议。

### 当前 Pipeline 7.0 的装配

内置 `automated-paper-formalization` 当前按以下顺序组装：

```text
RealInputImporter
  → Step1ExtractEvidencePlugin
  → Step2NormalizeClaimsPlugin ──可选/已配置 DomainTool──▶ 图片与实验 claim 语义补齐
  → Step3AnalyzeReasoningPlugin ─可选 DomainTool─────────▶ necessity / weakpoint 复核
  → Step4FormalizeReasoningPlugin 可选 DomainTool────────▶ 非确定性推理展开
  → Step5CompileGaiaIRPlugin ─────显式 official compiler─▶ gaia.ir
```

Step 1–4 不各自产生一套互不相关的领域文件，而是继承并生成同一 `formalization.json` 的下一 revision。每一步先做能够机械完成的工作，再在配置存在时调用语义工具；工具只能按稳定 ID 返回 `snapshot_patch`，合并后仍须重新通过完整领域校验。Step 5 更严格：没有显式 compiler 时返回 `waiting`；compiler 未声明 `metadata.contract_status=official`、Gaia 包不可用或官方解析/校验失败时均不会登记 `gaia.ir`。

人工审核目前不被硬编码进 Runner。Harness 已支持从 checkpoint fork 并向 child run 注入额外冻结输入，但当前领域插件尚未实现通用的 review-input reducer，因此“展示修改意见 → 合并审核结果 → 继续执行”还不是完整的端到端能力。现阶段系统保留 `needs_review` 状态和审计 Artifact；独立审核队列与 reducer 仍属于“当前暂缓”能力。

## 插件式设计原理

### 1. 稳定宿主，变化能力外置

Runner 只依赖最小协议，不依赖任何具体插件类：

```text
StagePlugin.run(StageContext) -> StageResult
DomainTool.invoke(ToolCallRequest) -> ToolCallResponse
ViewAdapter.project(store, artifacts, options) -> ViewDocument
```

这是一种依赖倒置：稳定的执行、存储和审计机制定义接口；易变化的论文处理规则、模型供应商、Gaia compiler 和可视化投影实现接口。增加一个新流程通常是新增或复用插件并修改 pipeline 配置，而不是在 Runner 中增加 `if pipeline == ...`。

### 2. 插件通过 Artifact 契约组合，而不是共享内存

Stage 之间没有直接方法调用，也不持有彼此对象。生产者声明 `ArtifactDraft.kind`，Runner 将其登记成带 ID、hash、媒体类型、producer stage 和 metadata 的 `ArtifactRef`；消费者通过 `find_all`、`require_one` 或 `latest` 按 kind 获取输入。

因此插件的真正兼容面是“Artifact kind + 文件 schema + provenance”，不是 Python 类的内部字段。这样带来三个结果：

- 可恢复：进程退出后可从 checkpoint 和持久化 Artifact 继续；
- 可复现：每次输入、配置和输出均有 hash，可以证明运行读取的确切版本；
- 可替换：只要保持同一 Artifact 契约，Stage 实现、模型或外部工具可以替换。

这也意味着插件化不能被误解为可以任意增加中间 JSON。新增 Artifact kind、schema 字段、状态或 checkpoint 语义都会扩大跨插件契约，必须先判断能否从现有真源派生；默认采用 zero-delta，优先复用、合并或删除。

### 3. 编排插件与能力插件分离

当前有三类主要扩展点，各自粒度不同：

|扩展点|负责什么|不负责什么|
|---|---|---|
|`StagePlugin`|一个可提交、可恢复的工作流阶段；决定读哪些 Artifact、产出哪些 Artifact|不直接修改 RunStore 或推进 checkpoint|
|`DomainTool`|一次可审计的语义/外部能力调用；返回 raw、normalized 或 error|不拥有领域状态，不绕过稳定 ID 合并与领域校验|
|`ViewAdapter`|把现有 Artifact 投影为统一视图模型|不修改 Artifact，不定义领域事实|

官方 compiler 复用了 `DomainTool` 的请求/响应与审计形式，但在 Step 5 额外施加 `official` 声明和 Gaia 运行时校验。这样既复用统一工具边界，又不会把普通模型生成的“看起来像 Gaia IR”的 JSON 当成官方产物。

### 4. 把不确定性锁在插件边界内

导入、hash、ID 引用闭包、revision 继承、索引和 schema 检查使用确定性代码。只有 claim 补写、图像理解、necessity test、induction/abduction 复核等无法可靠机械判断的任务才进入 `DomainTool`。

工具请求记录工具名、版本、operation、输入 Artifact ID/hash 和 parameters；响应同时保存 raw 与 normalized。领域层不直接信任 normalized 输出，而是按稳定 ID 合并、重新计算 revision history，并运行完整 validation。由此可以回答“模型看到了什么、返回了什么、哪些内容真正进入了领域真源”，也能在更换模型时重放和比较。

### 5. 配置是装配图，不是运行时真源

Pipeline JSON 只描述 stage 顺序、插件地址、options 和 `view_adapter`。Stage 名对 Runner 是不透明字符串，插件通过 `module:object` 延迟装载。`init` 后配置及其 SHA-256 被写入 Run；要改变顺序、工具或参数，应创建新 run 或 fork，而不是改正在运行的配置。

当前装载机制是进程内 Python 插件系统，不是带权限沙箱、依赖解析和远程发现的插件市场。插件拥有当前 Python 进程可见的能力，所以生产接入仍需代码审查、依赖固定和契约测试。未来即使引入 Registry 或分布式执行，也应保留现有 Artifact、结果状态和审计语义，避免上层 pipeline 与具体执行后端耦合。

### 6. 插件失败是数据，不是隐式控制流

插件只有三种结果：

- `succeeded`：产物可进入 checkpoint；
- `waiting`：缺少人工决定、官方 compiler 或外部条件，保存审计信息但不推进；
- `failed`：保存 Finding 和诊断 Artifact，停在当前 Stage 等待修复或重试。

业务问题通过稳定 Finding code 表达，未捕获异常也会被 Runner 结构化。这个设计使 CLI、Viewer、自动化检查和未来的调度器可以用同一套可观察结果工作，而无需解析日志文本或猜测插件是否“基本成功”。

## 快速开始

在仓库根目录执行：

```bash
cd agent
python3 -m pipeline_harness --help
RUN_DIR="$(python3 -m pipeline_harness init \
  --pipeline synthetic \
  --inputs tests/fixtures/synthetic/source_manifest.json)"
echo "$RUN_DIR"
```

`RUN_DIR` 中保存的是 `init` 返回的绝对 run 路径，例如：

```text
<repo>/outputs/harness_runs/run_...
```

后续命令直接使用这个变量。不要把文档中的 `<run-dir>` 一类占位符原样输入终端；zsh 会把尖括号解释为重定向符号。

```bash
python3 -m pipeline_harness run --run "$RUN_DIR"
python3 -m pipeline_harness status --run "$RUN_DIR"
python3 -m pipeline_harness view --run "$RUN_DIR"
python3 -m pipeline_harness search --run "$RUN_DIR" --query SYNTH_NODE_001
python3 -m pipeline_harness check --run "$RUN_DIR"
```

运行数据默认写入仓库的 `outputs/harness_runs/`。也可以在 `init` 时通过 `--runs-root` 指定其他位置。

## 外部输入

输入 manifest 只描述要导入的文件，不直接成为运行时数据源：

```json
{
  "artifacts": [
    {
      "path": "source.txt",
      "kind": "input.synthetic",
      "media_type": "text/plain",
      "metadata": {}
    }
  ]
}
```

相对路径以 manifest 所在目录为基准。`init` 会把文件复制到 Artifact Store，计算 SHA-256，并在 `inputs/manifest.json` 和 `run.input_artifacts` 中登记。后续 stage 只读 run 内副本，不再读取原路径。`run.json` 同时保存 `config_sha256`；配置被静默修改后，`run` 会拒绝继续，`check` 会报告错误。

## Viewer 怎么使用

### 模板与成品的区别

仓库中的：

```text
agent/pipeline_harness/view/viewer.html
```

是通用 HTML 模板，里面没有某次 run 的实际数据。不要把它当成最终可视化结果。

执行下面的命令后：

```bash
python3 -m pipeline_harness view --run "$RUN_DIR"
```

Harness 会调用当前 pipeline 配置的 `ViewAdapter`，把 Artifact 转换为 `ViewDocument`，再将数据注入模板，生成：

```text
<run-dir>/views/viewer.html
```

这个生成后的文件才是应该打开的 Viewer。它是一个自包含 HTML，不需要启动服务器。可以在 Finder 中双击，或在 macOS 终端执行：

```bash
open "$RUN_DIR/views/viewer.html"
```

同时还会生成：

```text
<run-dir>/views/view_model.json
<run-dir>/views/manifest.json
```

- `view_model.json`：Viewer 使用的节点、边、搜索文档和图层数据；
- `manifest.json`：adapter 版本、源 Artifact 及其 hash、Viewer 和 view model 路径。

### 页面操作

|操作|用途|
|---|---|
|搜索框|按 ID、中英文文本和 tag 搜索；精确 ID 优先|
|`Overview`|最终结果的折叠视图；typed weakpoint 显示为一条虚线非推理边|
|`Standard`|最终结果的完整形式化图；默认展开 Operator、private helper 和已确认推理边|
|`?dev=1`|开发者模式；隐藏双模式开关，改为选择 Step 1–5 审阅中间图|
|Layers|独立显示或隐藏某一类节点、边|
|拖动节点|临时调整当前步骤中的节点位置，连线会同步更新|
|Weakpoints|独立显示或隐藏 weakpoint 节点及其连线|
|重置布局|清除手动位置，恢复语义分层的默认排列|
|单击节点或边|在右侧 Property Sidebar 打开属性、weakpoint、校验和原文定位|
|双击折叠/展开边|只在当前页面局部展开或折叠对应 `fold_group`|
|点击搜索结果|定位对应节点并打开详情|

模式、步骤、拖动和折叠只影响页面展示，不会修改源 Artifact。正常入口始终使用最终 Step；开发者入口是在 URL 后追加 `?dev=1`，用于构建和回归阶段审阅 Step 1–5，不是面向最终使用者的第三种粒度。

边型规则与 Step 无关：已确认或已形式化的推理边使用实线，尚待确认的推理候选使用点线，registry、scope、source 等非推理边使用虚线。节点卡片显示短语义名称、类型/角色和摘要；完整稳定 ID 保留在悬浮提示及右侧详情中。

### 命令行搜索

不打开 Viewer 也可以搜索已经生成的 `view_model.json`：

```bash
python3 -m pipeline_harness search \
  --run "$RUN_DIR" \
  --query 合成条目
```

当前搜索只做确定性匹配：Unicode 规范化、ID 精确匹配、前缀匹配和中英文 substring，不使用 embedding。

### 领域数据投影

Viewer 不直接解释 `formalization.json` 或 `knowledge_index.json`，而是通过当前 pipeline 配置的 `ViewAdapter` 投影：

```text
ViewAdapter.project(artifact_refs, options) -> ViewDocument
```

Adapter 负责把领域数据映射成：

- `nodes`；
- `edges`；
- `search_documents`；
- `layers`；
- 每项的 `min_granularity`、摘要和详情。

内置自动形式化 pipeline 已提供该 Adapter；以后 pipeline 或领域 schema 调整时，修改 Adapter 即可，不需要修改 Viewer。

## Stage 提交与恢复

可以使用内置的 `synthetic-failure` pipeline。它的第二个 stage 会故意失败一次；再次执行同一个 `run` 命令时，会从最后成功的 checkpoint 继续：

```bash
FAIL_RUN_DIR="$(python3 -m pipeline_harness init --pipeline synthetic-failure)"
python3 -m pipeline_harness run --run "$FAIL_RUN_DIR"
python3 -m pipeline_harness run --run "$FAIL_RUN_DIR"
```

每个 stage 的固定输入为：

```text
run.input_artifacts + last_successful_checkpoint.artifacts
```

插件可用 `context.find_all(kind)`、`context.require_one(kind)` 和 `context.latest(kind)` 取输入。只有 `succeeded` 返回值中的产物会进入新 checkpoint；`waiting` 产物仅供审计，`failed` 产物仅供诊断。重试保持在同一 stage，不会把这两类产物传播到后续 stage。

## 从 checkpoint fork

`fork` 创建独立 child run，不修改 parent run：

```bash
CHILD_RUN_DIR="$(python3 -m pipeline_harness fork \
  --run "$RUN_DIR" \
  --checkpoint checkpoint_...)"
python3 -m pipeline_harness run --run "$CHILD_RUN_DIR"
python3 -m pipeline_harness check --run "$CHILD_RUN_DIR"
```

可用 checkpoint ID 来自 `<run-dir>/checkpoints/` 中的 manifest。child 的 `run.json` 保存 `parent_run_id` 和 `parent_checkpoint_id`；复制的 Artifact metadata 保存原 run、原 Artifact ID 和原 hash。

人工审核结果通过额外 input manifest 冻结到 child run，Runner 无需感知审核业务：

```bash
CHILD_RUN_DIR="$(python3 -m pipeline_harness fork \
  --run "$RUN_DIR" \
  --checkpoint checkpoint_... \
  --inputs /absolute/path/to/review_manifest.json)"
```

## 运行测试

不需要安装第三方依赖：

```bash
cd agent
python3 -m unittest discover -s tests -v
```

## Pipeline 配置

`--pipeline` 可以接受内置名称或 JSON 配置文件：

```json
{
  "pipeline_id": "example",
  "version": "1",
  "stages": [
    {
      "name": "any-stage-name",
      "plugin": "your_package.module:YourPlugin",
      "options": {}
    }
  ],
  "view_adapter": "your_package.views:YourViewAdapter"
}
```

Stage 名称对 Runner 是不透明字符串。只要插件之间的 Artifact 契约兼容，重命名或调整 stage 顺序只需修改配置。

## 自动论文形式化工作流

内置 pipeline `automated-paper-formalization` 提供通用的输入冻结与 Step 1–5 审核链，不依赖某篇论文的 claim ID、图片数量或章节结构：

```bash
RUN_DIR="$(python3 -m pipeline_harness init \
  --pipeline automated-paper-formalization \
  --inputs /absolute/path/to/input_manifest.json)"
python3 -m pipeline_harness run --run "$RUN_DIR"
python3 -m pipeline_harness view --run "$RUN_DIR"
python3 -m pipeline_harness check --run "$RUN_DIR"
```

成功运行依次产生：

|Step|Stage name|职责|
|---|---|---|
|1|`step1_extract_evidence`|导入冻结的 A、R 与指定的弱点细化候选，并保留来源关系|
|2|`step2_normalize_claims`|规范化 claim 角色并记录待补齐的实验语义|
|3|`step3_analyze_reasoning`|从来源标签生成候选推理单元，未确认判断保留为 `needs_review`|
|4|`step4_formalize_reasoning`|按方法论自动展开可证明安全的 deduction，其余保持 mixed coarse/formal 状态|
|5|`step5_compile_gaia_ir`|调用显式配置的官方 compiler；未配置时停在 `waiting`|

真实输入会先收敛为单个 `input.bundle`，并在内部保留每个来源 Artifact 的 ID、hash、媒体类型和内容。领域对象保留 revision 历史：每次内容变化都会记录版本号、内容 hash 和当时内容，Viewer 的开发者入口按 revision（对应 Step 1–4）选择图。Step 5 只接受官方 `gaia.ir`；`gaia.ir.candidate` 不会生成或导出。`knowledge.index` 只在 Step 5 成功后生成一次，并标记 `final=true`。每一步的 `formalization.validation` 与工具响应仍保留为审计产物。领域契约位于 `pipeline_harness/domain/`，版本为 `1.0.0`。

验证报告保留 validator 版本、被验证快照及哈希、逐规则检查统计和节点/边级 findings。每个 finding 都使用稳定语义对象 ID 定位目标，并可附带 JSON Pointer 和建议操作；例如断开的 Strategy premise 会定位到 `strategy:<strategy_id>:premise:<index>` 这条边。校验失败时仍保存 `formalization.snapshot.invalid` 与验证报告，供 Viewer 审核；校验通过时 Viewer 直接显示“通过校验”。Validation 图层在开发者模式中可开启；失败目标显示为红色，警告目标显示为黄色，节点和边均可点击并在右侧查看规则与位置。

确定性的导入、来源 ID 对齐、快照继承、索引、校验和可视化自动执行。需要语义判断的 claim 补写、necessity test、induction/abduction 复核或替代解释生成不得由 importer 猜测；它们通过 `DomainTool` / `SemanticReviewTool` 边界接入，原始响应与规范化结果分别保存。

Step 1–4 均可配置语义工具：

```json
{"options": {"tool_plugin": "your_package.review:Reviewer", "tool_parameters": {}}}
```

通用语义工具使用 `{"snapshot_patch": ...}`，均按稳定 ID 合并后重新执行完整领域校验。Step 5 可配置官方 compiler：

```json
{"options": {"compiler_plugin": "your_package.gaia:Compiler"}}
```

未配置官方 compiler 时 Step 5 返回 `waiting`，不生成 IR 或最终索引。插件必须明确声明 `metadata.contract_status=official`；Step 5 随后直接调用已安装 Gaia 的 `LocalCanonicalGraph`、`validate_local_graph` 和运行时 `IR_SCHEMA`，只有官方解析与校验通过后才登记 `gaia.ir`。Gaia 未安装、工具失败、contract 未声明或官方校验失败时，原始响应作为诊断 Artifact 保存，Stage 失败且不会推进 checkpoint。生产 Agent 中不存在 candidate compiler；隔离的 gold fixture 自带 test-only preview Stage，生产 pipeline 不配置也不登记 `gaia.ir.candidate`。

Viewer 的正常入口只显示最终结果的 `Overview / Standard`：Overview 将 Step 3 的 typed weakpoint 折叠为虚线非推理边，Standard 默认展开 Step 4 后的完整形式化图，已确认推理边使用实线。开发者模式 `?dev=1` 才提供 Step 1–5 选择及 Source、Identity、Registry、Scope、Validation 等审阅图层；每个 Step 不再重复设置两档视图。

`One Ticket to Win Them All` Figure 2 的可重复 gold-standard example 位于 [`../01_paper_sample_one_ticket/gold_standard/`](../01_paper_sample_one_ticket/gold_standard/README.md)。它使用标准 input manifest、正式 Step 1–5 插件、录制的人工语义审核工具、隔离的 test-only candidate compiler 和 Viewer；运行 `python3 01_paper_sample_one_ticket/gold_standard/run_example.py` 可重建中间 Artifact、测试预览输出和 Viewer。该 preview 不得作为官方 Gaia IR 或生产发布物。

## StagePlugin 契约

插件接收 `StageContext` 并返回 `StageResult`：

```python
from pipeline_harness.plugins import ArtifactDraft, StageResult


class ExamplePlugin:
    def run(self, context):
        output = context.work_dir / "result.json"
        output.write_text('{"ok": true}\n', encoding="utf-8")
        return StageResult(
            status="succeeded",
            artifacts=[
                ArtifactDraft(
                    path=output,
                    kind="example.result",
                    media_type="application/json",
                )
            ],
        )
```

插件只在本次 attempt 的工作目录写候选输出。Runner 负责把输出复制到 Artifact Store、计算 hash、记录 Finding，并仅在成功时创建 checkpoint。

## 当前暂缓

- 通用 LLM runtime、prompt 管理、embedding 和模型路由；
- proposal reducer 和独立人工审核队列；
- LangGraph/LangSmith、完整事件 replay、分布式锁、Registry 和数据库。

这些能力将在 `gold_standard/` 和 pipeline 契约稳定后以插件形式接入。
