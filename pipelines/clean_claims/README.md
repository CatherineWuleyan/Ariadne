# paper_graph2ir

把一篇科学论文（或者一段独立文本）的结论部分，转换成结构化、可独立核验的claim网络：每条论断是什么、彼此之间有什么支撑/举例关系、哪些地方还留有需要补充的具体例子。

本文档只说"给什么、出什么"，不涉及内部处理过程。

---

## 一、几种运行方式

项目提供4种入口，全部最终产出同一种结构的文件（见下一节），区别只在于**输入是什么**、**其中一步（标签打标）用哪种API调用方式**。

### 1. 单篇论文，全流程（API调用方式：流式/同步）

```
python run_full_pipeline.py <paper_id>
```

**输入**：`data/<paper_id>/graph.json` 必须已经存在——这是论文原始结构化数据，不是本项目产出的，需要你自己准备好。它的结构是：

```json
{
  "data": {
    "papers": [
      {
        "graph": {
          "nodes": [
            {
              "id": "...",
              "global_id": "...",
              "kind": "conclusion",
              "order": 0,
              "title": "...",
              "content": "这段结论的完整原文"
            }
          ]
        }
      }
    ]
  }
}
```
只有`kind`是`"conclusion"`的节点会被处理，一篇论文通常有多个这样的节点（对应论文里多处独立的结论性段落）。

**输出**：`data/<paper_id>/claims_final.json`（结构见下一节）。

### 2. 一篇或多篇论文，全流程（API调用方式：Batch API）

```
python run_multi_paper_step1a_batch.py <paper_id1> <paper_id2> ...
python run_multi_paper_step1a_batch.py --file paper_ids.txt
```

**输入**：跟方式1完全一样（每个`paper_id`各自的`graph.json`），区别是可以一次给多篇论文，每篇论文互相独立处理、互不等待。

**输出**：每篇论文各自的`data/<paper_id>/claims_final.json`。

### 3. 一段独立文本，全流程（API调用方式：流式/同步）

```
python run_single_text_pipeline_sync.py "一段文本"
python run_single_text_pipeline_sync.py --file mytext.txt
```

**输入**：一段文本本身，不需要预先准备任何文件——程序会自动把这段文本包装成一个只有1个"结论节点"的合成论文，`paper_id`按当前时间自动生成（格式`text_YYYYMMDD_HHMMSS_微秒`，北京时间）。

**输出**：`data/<刚生成的paper_id>/claims_final.json`，运行时会打印这次生成的`paper_id`。

### 4. 一段独立文本，全流程（API调用方式：Batch API）

```
python run_single_text_pipeline_batch.py "一段文本"
python run_single_text_pipeline_batch.py --file mytext.txt
```

跟方式3完全一样，只是标签打标这一步走Batch API。

---

## 二、输出结构与语义

不管走哪种方式，最终产出的都是同一个文件：`claims_final.json`，顶层结构是：

```json
{
  "claim": [...],
  "note": [...],
  "relation": [...]
}
```

三个部分分别是独立的列表。全文用到的编号（claim编号、note编号）都是**整篇论文范围内连续编号**，不是按每个结论节点单独编号——也就是说，同一篇论文里，claim编号1、2、3……一路编下去，不会因为跨越了不同的结论节点就重新从1开始。

### 1. `claim`——论文里每一条独立、可核验的论断

```json
{
  "conclusion": "conclusion_4",
  "text": "The combined module of Average of Pruning (AoP) is applied without modifying existing post-hoc OOD scoring methods.",
  "is_pure_data": false,
  "needs_more_context": [],
  "number": 20
}
```

| 字段 | 含义 |
|---|---|
| `conclusion` | 这条claim原本出自论文里的哪一个结论节点（仅用于溯源，不影响编号） |
| `text` | 这条claim的最终文本——已经过语义补全，结合这个文本里引用到的note内容，能独立于上下文被理解（如果文本里提到了"note N"，需要一并参照那条note才算完整） |
| `is_pure_data` | 这条claim是不是纯粹的实验数据罗列（比如一串准确率数字），不包含需要解读的论断 |
| `needs_more_context` | 一个字符串列表，列出这条claim里仍然存在、但结合全文也无法确定含义的术语/指代；正常情况下是空列表`[]` |
| `number` | 这条claim在全文里的编号（整篇论文连续编号） |

### 2. `note`——被claim引用、但本身不构成独立论断的内容

```json
{
  "conclusion": "conclusion_2",
  "text": "elaboration of IMP regime: In the specific iterative magnitude pruning (IMP) regime adopted for Trojan analysis...",
  "number": 1
}
```

| 字段 | 含义 |
|---|---|
| `conclusion` | 这条note原本出自哪个结论节点 |
| `text` | note的内容。分两种写法：<br>① 直接原样照抄论文原文；<br>② 如果这段内容是在解释某个具体术语或某条claim，前面会加一句`"elaboration of ...: "`说明它在解释什么，再接原文内容。 |
| `number` | 这条note在全文里的编号 |

**只有被某条claim的正文实际引用过的内容，才会出现在`note`里**——不是论文里所有的辅助性文字都会被收进来，只收真正有claim指向它的那部分。claim的`text`字段里，如果提到"参见note 3"这种指向，指的就是这里编号为3的这一条。

### 3. `relation`——claim之间的逻辑/支撑关系

```json
{
  "conclusion": "conclusion_3",
  "connects": [5, 4],
  "expression": "[5] 是 [4] 的例子或证据"
}
```

| 字段 | 含义 |
|---|---|
| `conclusion` | 这条关系所属的结论节点（一条关系连接的claim必然都属于同一个结论节点） |
| `connects` | 一个整数列表，列出这条关系涉及的全部claim编号 |
| `expression` | 用中文描述这条关系具体是什么，见下方两种形式 |

`expression`有两种可能的写法：

1. **逻辑关系**：只由以下几种基本单元组成——`且`、`和`、`与`、`或`、`非`、`推出`、`等价`、`矛盾`这8个连接词，圆括号`(` `)`用于分组，以及方括号包住的数字`[N]`表示claim编号，不会出现任何其它字符。比如`"([6] 且 [9]) 推出 [11]"`表示claim 6和claim 9同时成立，可以推出claim 11成立；`"[5] 与 [6] 矛盾"`表示claim 5和claim 6相矛盾。
2. **例子/证据关系**：`"[m] 是 [n] 的例子或证据"`（只有一个目标时）或`"[m] 是 ([n1] 和 [n2]) 的例子或证据"`（涉及多个目标时）——表示claim m是对claim n的支撑：可能是claim n的一个具体例子，可能是支撑claim n成立的证据，也可能是claim n更具体的表述方式。

   > 为什么不进一步区分"例子"和"证据"这两种关系：因为这个区分对还原底层的推理关系没有帮助。"A是B的例子"，在底层推理上可能是A推出B（比如A具备B所描述的一般性质里的某个具体数值），也可能反过来是B推出A（比如A是B里某个一般条件的具体实例化）；"A是B的证据"同样可能是演绎证据、也可能是溯因证据，两种推理方向都存在。也就是说，光凭"是例子"还是"是证据"这个标签本身，并不能直接推出两者之间底层推理关系的具体形式，所以干脆不区分，统一用一种表达方式。

---

## 三、Batch 和 流式 的区别

项目里"标签打标"这一步（把结论原文切分成带标签的片段）需要调用API，这一步存在两种实现，其余处理步骤两种方式下完全一样、都是流式调用：

| | 流式（同步） | Batch API |
|---|---|---|
| 调用方式 | 发一条请求、实时等它返回结果 | 一次性提交一批请求，稍后再取结果 |
| 返回时间 | 几秒到几十秒 | 官方承诺24小时内，实际大多数情况下更快，但没有实时保证 |
| 单价 | 标准价格 | 标准价格的50% |
| 适合场景 | 单篇论文、需要立刻看到结果 | 批量处理多篇论文，不着急拿到结果、想省钱 |

"标签打标"这一步是全流程里对token消耗量最大的一步，所以只把这一步换成Batch API，就能带来接近腰斩的整体成本下降（其余步骤仍是流式调用，占比相对小）——这也是为什么下面"部分batch"模式的成本明显低于"全流式"，而不需要把全部步骤都换成Batch API。

---

## 四、成本估算

以下是处理**一篇论文**（不是一段文本）全流程的大致成本区间，实际会因论文长度、结论节点数量、每一步遇到需要升级重试的次数而有所浮动：

| 模式 | 预估成本 |
|---|---|
| 全流式（`run_full_pipeline.py`） | 约 **1 ~ 1.5 美元** |
| 部分batch（`run_multi_paper_step1a_batch.py`，仅标签打标走Batch API） | 约 **0.5 ~ 0.8 美元** |

处理一段独立文本（而不是一整篇论文）的成本远低于此——因为只有1个结论节点，不是论文通常包含的多个结论节点，token消耗量小得多。
