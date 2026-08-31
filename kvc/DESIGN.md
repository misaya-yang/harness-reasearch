# KVC 闭环实验框架设计 (v0.9)

> 依据：2026-08-31 粘贴的 KVC 计划全文（Knowledge Activation – Verification – Control）。
> 地位：本文件是 `kvc/` 目录一切代码的构建规格。与计划原文冲突处，以"显式偏离清单"（§10）为准并逐条注明理由。
> 模型固定：`qwen3.8-flash`（thinking off 为默认基线；thinking 消融仅在 R8 指定臂开启）。
> 端点：阿里云新加坡 OpenAI 兼容端点（沿用 `agent_config/models.json` 的 `dashscope-intl` 配置，`api: openai-responses`）。

---

## 1. 定位

旧轮次（H0–H4、PEAC、CTR、termination-control）证明了：

- 强迫反思（PEAC）不产生恢复；
- 事件账本（CTR）改善可观测性但不建立正确性；
- 已经正确的轨迹不会自行停止。

KVC 把修复目标锁定在四个转换边界：

```
知识 →(K) 可执行假设 →(D→I) 源码承诺 →(V) 外部证据 →(C) 停止/交付
```

三个组件职责互不重叠：

| 层 | 职责 | 实现位置 |
|---|---|---|
| K | 决策边界上的事件触发知识激发（fresh-context checkpoint） | harness 旁路新会话 |
| V | 可执行行为验证（冻结 verifier，epoch 绑定，限次） | pi 内注册工具 + 外部沙箱执行 |
| C | 确定性进度状态（GPS）+ 停止规则 + incumbent 保全 | harness 状态机 |

纪律继承旧仓：证据分级（FACT/OBSERVATION/INFERENCE/HYPOTHESIS/PROPOSAL）、model-ID gate、
key 只进环境变量、配置冻结 + sha256 manifest、串行执行、完整性失败即停批。

---

## 2. 运行载体（决策）

**决策：Python 控制器 + pi 子进程（actor 走 RPC 模式，探针走 print 模式）。**

理由：

1. 旧轮次的全部运行纪律（420s watchdog + killpg、资源监控、隔离环境、evaluator、
   顺序平衡、批停语义）都是 Python，直接复用；
2. KVC 的 K/V/C 三层全在 agent 循环*外部*，不需要侵入式扩展；
3. pi 的 RPC 命令面经源码核实完备：`prompt`（含 `streamingBehavior`）、`steer`、
   `abort`、`set_thinking_level`、`get_messages`、`get_session_stats`、`fork`；
4. `steer` 的语义（当前 assistant turn 的工具批次全部完成后注入）与计划对 KAC
   注入时机的要求逐字吻合；
5. 唯一需要进入 pi 进程的代码是 `validate_current_patch` 工具——用一个外部路径的
   扩展文件加载（`--extension <kvc/extensions/kvc-validate.ts>`），pi 源码树零改动。

备用载体：若 M0 发现 RPC 不满足需求（事件形状/steer 时序异常），退回
SDK 进程内驱动（pi 根 tsconfig 有完整 `paths` 映射，`tsx` 可从源码直接跑）。
两条路共用同一套 Python 记录/评估代码，切换成本已压到最低。

### pi 源仓不可变保证

- pi 仓（`~/misaya_project/Agent_projects/pi`，HEAD `853a80d26`）全程**只读使用**：
  只用 `git archive` / `git diff` / `git cat-file` / `tsx` 执行；
- 不提交、不 checkout、不编辑、不在其工作树添加任何文件；
- 唯一写操作是 `npm install --ignore-scripts`（其 AGENTS.md 明确允许的本地装配）；
- 所有实验改动发生在一次性物化工作区（`git archive base_commit` → 临时目录 → 独立
  git 仓），用完即弃或归档为只读。

---

## 3. 组件契约

### 3.1 GPS（harness 侧确定性状态机，模型不可写）

持久化字段全部是机器事实。模型永远不能填写、确认或修改任何 GPS 字段。

```json
{
  "objective_anchor": "<冻结的任务 prompt 摘要>",
  "phase": "localize | implement | validate | deliver",
  "elapsed_seconds": 231,
  "remaining_seconds": 189,
  "mutation_epoch": 1,
  "current_validation": {
    "epoch": 1,
    "scope": "focused_behavior",
    "result": "pass | fail | none",
    "counterexample": null
  },
  "incumbent_validated_epoch": 1,
  "delivered": false
}
```

phase 判定（确定性规则）：

- `localize`：mutation_epoch == 0；
- `implement`：mutation_epoch ≥ 1 且当前 epoch 无验证结果；
- `validate`：当前 epoch 有验证结果（无论 pass/fail）且未交付；
- `deliver`：harness 收到交付信号或运行终止。

GPS 只在触发时刻以紧凑文本块注入（随 KAC card 或独立 steer），不做每轮广播——
避免把进度状态变成持续 narrative。

### 3.2 Mutation 跟踪（epoch 判定）

不依赖工具名。每次 `tool_execution_end`（bash/edit/write）后：

1. 工作区 `git status --porcelain` + production 路径内容哈希；
2. 有新增 production 内容变化 → `mutation_epoch += 1`，打内部标记
   （不提交，只记快照哈希；incumbent 提交是另一机制）；
3. 路径分类：任务声明 `production_paths`（默认 = 除 `scratch/`、测试夹具外全部）；
   `scratch/` 内的变化永不计数（探针区）；
4. 每个 epoch 记录：起止时间、触发工具、diff stat、哈希。

### 3.3 `validate_current_patch()` 工具（V 层）

注册：`kvc/extensions/kvc-validate.ts`，经 `--extension` 加载；配置走环境变量
（`KVC_VALIDATOR_DIR`、`KVC_VALIDATION_BUDGET`、`KVC_WORKSPACE_ROOT`）。

行为：

1. 读取当前 mutation_epoch（工作区标记文件，harness 维护）；
2. 在禁网沙箱中运行**冻结的在线 verifier 套件**（行为不变量导向，与任务同目录）；
3. 返回且仅返回：

```json
{
  "mutation_epoch": 2,
  "validation_epoch": 2,
  "scope": "focused_behavior",
  "result": "pass | fail",
  "counterexample": "Windows drive-root path remains absolute | null",
  "applies_to_current_source": true
}
```

4. 调用限额（默认 2 次）在工具体内强制；超限返回结构化错误说明限额已用尽；
5. 泄漏禁令：不返回 gold helper 名、gold patch、gold 错误文案、实现形状、
   完整测试源码。counterexample 只能取自测试自身输出的行为描述；
6. 验证执行不计为 mutation（只读工作区；若测试产物污染工作区，先 `git stash` 后恢复）。

### 3.4 KAC 触发器与 decision card（K 层）

三个触发条件全部确定性，不允许按模型语义临时决定是否触发：

| # | 条件 | 说明 |
|---|---|---|
| T1 | elapsed ≥ 35% 预算 且 mutation_epoch == 0 | 知识→承诺转换失败探测 |
| T2 | 当前 epoch 首次 validation fail | 证据→修正边界 |
| T3 | 当前 epoch validation pass 后，又发生 ≥ N 次工具调用且未交付（N 默认 4，冻结） | 正确但不停止探测 |

触发后流程：

1. 启动 fresh-context 探针：独立 `pi --print` 子进程，干净上下文，
   thinking 级别按臂配置（默认 off；R8 消融臂可单点开）；
2. 探针输入**只有**：原始任务、当前相关源码（有界：全部已变 production 文件 +
   actor 最近 K 次读取过的文件，字节上限冻结）、当前对 base 的 diff、
   原始外部 observation、当前 GPS。**不提供** actor 自己写过的任何假设/总结；
3. 探针必须输出严格 JSON decision card：

```json
{
  "invariant": "最可能被违反的工程不变量",
  "edit_surface": "应执行该不变量的精确源码位置",
  "minimal_change": "最小可逆源码修改的描述",
  "falsifier": "能推翻该修改的一个观察或测试",
  "next_action": "mutate | probe | deliver"
}
```

4. card 经 RPC `steer` 注入 actor（语义 = 当前工具批次完成后送达）；
5. card **replace-in-place**：harness 日志里新 card 覆盖旧 card 的引用；
   注入本身是一次性消息，不构成累计 ledger；
6. 探针预算上限冻结（默认 120s / 一次模型调用结构），探针失败不阻塞 actor。

提示词模板按计划原文的五行 checkpoint 指令实现，存放于 `configs/prompts/`，
冻结后逐字节 sha256 入 manifest。

### 3.5 Incumbent 保全（C 层）

工作区物化时即独立 git 仓（pi_tasks.prepare 已有 "benchmark base" 初始提交）：

1. 每次当前 epoch validation **pass** → `git add -A && git commit -m "kvc incumbent epoch <e>"`，
   打 tag `kvc/incumbent-<e>`，记入 `incumbent_validated_epoch`；
2. 后续探索在原地继续（不切 worktree——pi 的 cwd 绑定使 worktree 切换代价高且
   打断会话；见 §10 显式偏离）；
3. 运行终止时（正常交付除外）若存在 incumbent 且最终工作区未通过评估 →
   `git reset --hard` 到最近 incumbent tag，重新评估；
4. 该结果单独计为 **workspace rescue**，任何报表中与 strict autonomous
   completion 分列，不得合并。

### 3.6 停止规则（soft trigger，非 hard gate）

- validation pass 后的 T3 触发时，card 之后追加交付压力文本（计划原文）：

  > The current source state has post-mutation validation evidence.
  > Continue only if you can name: 1. an explicit unresolved user requirement,
  > and 2. one bounded test whose result could change the patch. Otherwise deliver now.

- 无 mutation（T1）触发后两步内应出现 production mutation 或一个明确能改变
  patch 选择的区分性观察——只记录，不强制（避免 PEAC 式 admission block）；
- 420s 硬 cutoff 由 watchdog 执行（继承旧实现：`start_new_session` + killpg）。

---

## 4. KAA（Experiment 0）：Counterfactual Checkpoint Assay

在干净轨迹的三个时刻保存 checkpoint：C0（任务加载后）、C1（初步诊断或 35% 预算）、
C2（cutoff 前）。每个 checkpoint 派生四种探针（全部独立 `pi --print` 子进程）：

| 探针 | 内容 | 工具面 |
|---|---|---|
| D 诊断 | 违反了什么不变量 / 最可能位置 / 区分性证据 | read-only |
| I 实现 | 隔离工作区副本中直接生成最小 patch → 跑冻结评估器 | edit/write + evaluator |
| S 选择 | 3–4 个匿名候选（1 行为正确 + 2–3 表面合理实错）→ 选哪个 + 给出区分测试 | read-only |
| V 校准 | 对当前 patch 给出通过概率 + 最可能推翻它的测试 | read-only |

S 探针的候选集在任务校准期构造：正确候选 = gold 或行为等价实现；干扰项 =
任务级"合理但错"变异配方生成（见 §5 校准）。候选匿名化、顺序随机化（固定种子）。

三个核心派生指标：

```
Activation Gap  = P(fresh probe patch 通过 且 原轨迹未部署)
Grounding Error = P(自评判断 ≠ 行为评估结果)
Control Loss    = 首个持续通过 patch 之后的时间/token 消耗
```

---

## 5. 任务体系与冻结切分

### 5.1 现有资产

28 个历史任务行（`experiments/pi_trajectory/tasks/*.jsonl`），全部 base/gold
commit 已验证存在于本机 pi 克隆。其中 6 个任务族已有冻结行为测试
（`behavioral_regrade/tests/*.behavior.test.ts`，README 记录 base-fail/gold-pass）。

### 5.2 切分（本设计决定，写入冻结清单后不得改动）

| 集合 | 来源 | 用途 |
|---|---|---|
| DEV-6 | v3 中已有行为测试校准的 6 个任务族 | KAA + R8–R10 机制调试（旧轮次用过，仅作 dev，合理） |
| R7-6 | **新挖掘**：46 个新提交（a470b121b..HEAD 及之后）中的单点修复 | R7 holdout（全局未见） |
| R11-12 | 继续从未用提交挖掘；不足时向前扩展到从未出现在任何任务行的修复提交 | R11 冻结 holdout |
| 禁用 | 全部 28 个旧任务行 | 永不进入 R7/R11 |

### 5.3 挖掘与校准协议

每个新任务行必须通过：

1. 自动门：base 上冻结测试失败、gold 上通过（`pi_tasks.evaluate` 变体）；
2. 任务行字段：`production_paths`、`scratch_hint`、`verifier_suite`、
   `online_budget`（validate 调用限额）、`timeout_seconds: 420`；
3. 四道校准（计划要求，进 R7 前完成）：
   - base patch 必须失败；
   - gold patch 必须通过；
   - ≥2 个独立正确实现必须通过（不同温度/不同提示生成后人工筛）；
   - ≥2 个 plausible-but-wrong 变异必须失败（同时充当 S 探针干扰项）。

---

## 6. 运行目录与冻结

```
kvc/results/<round>/<batch>/<task>__<arm>__<seed>/
├── workspace/            # 物化工作区（含 .git，incumbent tag）
├── events.jsonl          # RPC 事件 + 墙钟时间戳（turn/message/tool/usage）
├── gps.jsonl             # GPS 状态迁移日志
├── epochs.jsonl          # mutation epoch 记录（哈希、diff stat、时间）
├── kact/                 # 每次触发的探针 prompt、card、注入时刻
├── validation/           # validate 调用记录 + 沙箱输出摘要
├── evaluation/           # agent.patch、hidden 评估、（可选）rescue 评估
├── run-manifest.json     # 配置冻结哈希、pi HEAD、端点、模型、返回 responseModel
└── runtime.jsonl         # 资源采样（10s 粒度）
```

`run-manifest.json` 记录全部冻结输入的 sha256（prompt 模板、触发阈值、
verifier 字节、任务行、顺序表）+ 端点返回的实际 `responseModel`；
与配置不符 → 该 run 判 `MODEL_MISMATCH` 无效（继承 model-ID gate）。

---

## 7. 指标

继承旧轮：ever-pass、time-to-first-pass、strict completion、false completion、
tokens/calls、`*_at_cutoff` 状态分类。

新增（按计划）：

- time-to-first-mutation、210s 前是否产生 production mutation；
- post-pass tail（首个持续通过后的 calls/tokens/seconds）；
- correct-to-wrong flip rate（reflection/verifier 把正确状态改错）;
- checkpoint 后两步内 relevant mutation rate、probe-to-action conversion；
- Activation Gap / Grounding Error / Control Loss（§4）；
- workspace rescue 计数（单独列）。

---

## 8. 阶段计划

| 阶段 | 内容 | 门槛 |
|---|---|---|
| M0 | provider 冒烟（端点×模型×thinking 透传）、`--extension` 外部路径验证、RPC+steer 时序验证、pi_tasks 改道、依赖缓存重建、DEV-6 之一单任务 native 全量录制 | **需 key** |
| M1 | KVC 内核离线实现：gps / mutation_tracker / incumbent / kact / kvc-validate.ts + faux 确定性测试（无 provider） | 无需 key |
| M2 | KAA（D/I/S/V 探针）+ DEV-6 native 轨迹 → 三个派生指标 | M0+M1 |
| M3 | R7：GPS 单因素，6 任务 × 2 臂 × seed，counterbalanced，冻结 | 任务挖掘完成 |
| M4 | R8：KAC 三臂（GPS only / 等成本 generic reflection / fresh-context KAC）+ thinking 单点消融 | R7 分析 |
| M5 | R9：verifier 三臂（自评 / fresh-context 同模型评估 / 可执行 verifier，限 2 次） | M4 最优臂 |
| M6 | R10：停止 + incumbent 三臂（原生 / finalization prompt / + incumbent rollback） | M5 |
| M7 | R11：完整 KVC 冻结 holdout（12 簇 × 2 seed，串行，420s，GO 标准见计划 §五） | 全部参数冻结 |

GO/NO-GO：每阶段结束出分析报告，沿用 `NO_GO_*` 决策标记传统；
R11 GO 标准六条照计划原文执行（≥5 簇独立赢、0 语义回归、sign test p≤0.05、
无 false completion、tokens ≤1.35×、≥2 项收益来自 strict/earlier sustained pass）。

---

## 9. 资源治理

- 全程**串行**执行（含 dev 轮）；无并行批；
- 单 run：420s watchdog（评估 90s 上限），`start_new_session` + killpg 收尾；
- 资源监控：10s 采样后代进程 RSS，上限沿用 2.5GB（本机内存充足，上限可配置
  但默认保守），越限 → `RESOURCE_FAILURE` 并终止该 run；
- 探针子进程：独立预算上限（默认 120s），探针失败不拖累 actor；
- 依赖缓存：物化工作区的 node_modules 走硬链接缓存（重建旧
  `/Users/Shared/pi-peac-experiment` 的等价物于 `kvc/.cache/`），避免每任务全量安装。

---

## 10. 显式偏离清单（相对计划原文）

| # | 偏离 | 理由 |
|---|---|---|
| 1 | incumbent 用 commit/tag + reset 实现，不用独立 worktree | pi 的 cwd 绑定；worktree 切换打断会话且引入双工作区同步问题。保全语义不变（恢复最近 validated 状态），rescue 单独计 |
| 2 | KAC card 经 RPC `steer` 注入，而非直接改写上下文 | `steer` 的送达时机（工具批次完成后）与计划要求一致，且不破坏轨迹可审计性 |
| 3 | 运行载体为 RPC 子进程而非 SDK 嵌入 | 最大化复用旧轮 Python 纪律；SDK 为备案（§2） |
| 4 | "production mutation" 按路径分类 + 内容哈希判定，不按工具名 | agent 可用 bash 改文件；工具名判定会漏报 |

---

## 11. M0 验证清单（拿到 key 后逐项执行）

1. `pi-test.sh --print -p "Say exactly: ok" --provider dashscope-intl --model qwen3.8-flash --thinking off`（key 走 `ANTHROPIC_AUTH_TOKEN` 环境变量）；
2. `--extension /abs/path/kvc/extensions/kvc-validate.ts` 外部路径加载可用；
3. `--mode rpc` 事件流形状与旧 `--mode json` 一致；运行中 `steer` 命令按"工具批次完成后"送达；
4. `thinking off` 与 `set_thinking_level medium` 下请求体 `enable_thinking` 字段正确（抓一次请求日志确认透传）；
5. `pi_tasks.py` 改道本机 `PI_REPO` 后，DEV-6 任一任务物化 + 依赖缓存 + hidden 评估全链路通过；
6. 单任务 native run 产出完整 `events.jsonl`（含 usage、墙钟、request/first-token/last-token 时间）。

任何一项失败 → 记录事实、评估备用载体（§2），不得静默替换方案。
