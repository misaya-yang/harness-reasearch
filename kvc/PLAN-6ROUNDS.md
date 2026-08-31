# KVC 六轮实验计划（2026-08-31 起）

前提（已有资料/资产）：

- 任务：v3 套件 10 任务（`experiments/pi_trajectory/tasks/pi_coding_tasks_v3.jsonl`），
  全部 base/gold commit 已在本机 pi 克隆验证；校准基建就绪（base/gold vitest 双跑）。
- 已校准并有干净基线数据的任务：`pi-retry-attempt-timeout`
  （5 次干净 native 运行，**4/5 终态通过**冻结行为验证器；旗舰 = r3-110139，
  epoch 19 原生达到通过补丁）。
- 泄漏封堵已完成：sanitized task.json + base 镜像 + 预计算 hidden-tests.patch +
  manifest 无 pi_repo + 升级审计（含邻居 run 访问检测）。每个结论只计 **clean** 层级运行。
- 臂：native（对照）、KAC（`run_kac.py`/`kact.py`，已离线演练，未 live）、KAA（未建）。
- 资源：并发上限提至 3 native（树峰值 ~2.1GB/运行，预算 7.5GB），校准并发 2。

核心命题：S = D∧I∧V∧T。失败模式 = 声明性-程序性间隙。
KAC 预期作用点：T1（未突变空转→激活）、T2（验证失败→重定向）、T3（通过后滞留→交付压力）。

---

## Round 1 — KAC 首次 live（本任务，n=3）

- 内容：`run_kac.py --task pi-retry-attempt-timeout` ×3（预算 420s，与基线同参数）。
- 验证目标：探针真实点火（T1/T2/T3）、决策卡解析率、steer 接受率、探针→后续突变转化。
- 对比：与 5 次干净 native 基线（4/5 通过）比较通过率、时间、epoch 数。
- 通过判据：≥1 次真实探针点火且卡注入成功；无运行因探针故障受损（探针错误只记录）。

## Round 2 — Native 基线扩展到 DEV 任务

- 内容：`run_batch --calibrate-uncalibrated`（补齐 v3 其余任务校准），
  然后选 **3 个校准干净**的新任务各跑 native n=2（并发 3）。
- 产出：跨任务 native 通过率/失败模式分布（预算耗尽 vs 早停 vs 错误收敛）。
- 依赖：校准需 base/gold 双跑 vitest；校准失败的任务跳过并记录原因。

## Round 3 — KAC 在 DEV 任务上（n=2/任务）

- 内容：Round 2 同 3 任务跑 KAC n=2。
- 产出：跨任务 native vs KAC 配对比较（通过率、首次通过 epoch、交付延迟）。

## Round 4 — KAA 探针（Experiment 0）

- 内容：建 `kvc/harness/probes.py`（DESIGN §4）：干净轨迹的 C0/C1/C2 检查点，
  派生 D（诊断，read-only）/ I（实现，隔离副本+冻结评估器）/ S（选择，匿名候选）/
  V（校准，通过概率）探针。
- 材料：Round 1–3 积累的干净轨迹（含通过/未通过两类）。
- 产出三个指标：Activation Gap（新探针补丁通过且原轨迹未部署）、
  Grounding Error（自评 ≠ 行为评估）、Control Loss（首个持续通过补丁后的消耗）。
- S 探针候选集：正确候选 = gold 或行为等价；干扰项 = 任务级"合理但错"配方
  （本轮先手工配方，机械化留待后续）。

## Round 5 — 机制消融

- 内容：两个 KAC 变体（各在本任务 + 1 个 DEV 任务，n=2）：
  - (a) 去掉 T3 交付压力（只保留 T1/T2）；
  - (b) 空卡对照：触发时注入无探针内容的格式卡（隔离"新鲜上下文信息"与"单纯打断"效应）。
- 产出：把 Round 1/3 的增益归因到具体组件。

## Round 6 — 冻结清单与总结分析

- 内容：冻结切分清单（任务套件/提示词/配置，全部 sha256 入 `kvc/freeze-manifest.json`）；
  全量统计汇总（按臂×任务×轮次，仅 clean 层级）；结论报告
  （命题支持度、失败模式分类、KAC 增益估计与置信区间、局限性清单）。
- 产出：可复现冻结态 + 最终报告（为 R7 holdout 运行做准备）。

---

## 执行纪律

- 每个运行必须过 `audit_leaks`；仅 clean 层级计入统计，污染运行标 TAINT 保留作分析。
- 资源护栏：`run_batch` RSS 闸门 + watchdog；校准与 native 错峰。
- 每轮结束：分析写入 `kvc/analysis/round-<N>.md`，随代码提交推送。

---

## 附录 A — Agent Hysteresis 框架的并入（2026-08-31，外部猜想）

外部猜想：**Agent Hysteresis / path invariance**。当两个轨迹到达*完全相同*的
充分状态 `s`、目标与工具也相同、仅历史 `h` 不同时，合理策略应满足
`π(a|h_A,g) ≈ π(a|h_B,g)`；若仍因因果无关的历史而偏移，则存在滞后
`H(s)=E[D_JS(π(·|h_i),π(·|h_j))]`。命题：历史内容可以全部真实无毒、当前状态已充分，
模型仍无法遗忘因果无关历史——这是 policy property，不是 prompt 工程。
架构推论：**History is storage; state is computation**（transcript 是日志，不是状态）。

**本计划已天然具备检验该命题的全部构件**，无需另起炉灶：

| 猜想构件 | 本计划对应物 |
|---|---|
| 充分状态 `s` | 检查点树 `tree-C0/C1/C2`（`materialize_checkpoint`，同态重建） |
| 历史 A（原轨迹） | 干净轨迹本体（events + 后续突变/自评） |
| 历史 B（最小历史） | 新鲜上下文 KAA 探针（只见状态、无轨迹记忆） |
| 策略输出 | D 探针诊断 / V 探针判定 / KAC 决策卡 |
| 世界判分（无需在线） | 冻结行为验证器 + 已校准 base/gold |

据此，把 Round 4/5 的派生指标扩展为滞后测量（无需在线打分、单次前向/单探针即可）：

- **Diagnosis divergence**：同一棵树 `s` 上，轨迹内隐式诊断（下一次突变命中面）
  vs 新鲜 D 探针诊断的一致率；分歧即滞后的直接证据。
- **Verdict divergence**：同一补丁状态上，轨迹内自评（validation 时的继续/交付决策）
  vs 新鲜 V 探针 pass/fail 判定的分歧率（= Grounding Error 的滞后解读）。
- **Action flip / retention**：给定正确动作 `a*`（= gold 的下一编辑面），
  `P(a*|h)` 随无关历史长度的衰减曲线——用 C0/C1/C2 三个历史深度近似采样。

**KAC 决策卡即"Canonical State"基线**：它把完整轨迹替换为
`GPS + diff + bounded source`（状态投影），正是外部猜想中
Full-History vs Canonical-State 消融的一端。因此 Round 1/3 的 KAC vs native
对比，同时就是"状态投影能否消除滞后"的检验。

**执行影响**：Round 4 分析必须输出上述三类分歧/衰减指标（写入
`analysis/round-4.md`）；Round 6 总结把"滞后是否显著、状态投影是否消除滞后"
列为与 S=D∧I∧V∧T 并列的头号结论。若 Round 4 显示等效状态下分歧 <2%，
则按外部猜想的判据"杀项目"（转向其它方向）；若分歧 15–40%，强化该框架为主叙事。
