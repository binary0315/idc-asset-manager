# 下一步开发 PRD：工单系统 MVP

| 项 | 内容 |
|---|---|
| 文档类型 | PRD（Product Requirements Document，产品需求文档） |
| 版本 | v0.1（草案） |
| 状态 | 待评审 |
| 日期 | 2026-11-23 |
| 上游文档 | `docs/L11-DOA-需求差距.md`（Gap Analysis）、用户提供的 L11 DOA 需求原文 |
| 目标读者 | 产品/开发/测试 |

## 1. 背景与问题

`docs/L11-DOA-需求差距.md` 的结论是：当前系统完成度约等于 L11 DOA 需求的 **Inventory 底座 1/3**，工单系统整体缺失，维护自动化基本没有。

需求描述的闭环是：

> 检测异常 → 建单 → 派工换件 → 库存出入库 → 更新 master list → 再检测

当前每一环都靠人工录入，缺少把各环节串起来的**事件源**。三系统里，**Ticketing 是枢纽**——需求明确"生成的 record 需与 ticketing 和 inventory 联动，更换后的 SN 需要录入两个系统"。没有工单，Inventory 的自动出入库就无从触发，Maintenance 的 record 也无处归档。

因此下一步的核心不是继续堆台账功能，而是**补上枢纽**：先做一个不依赖任何外部系统、能跑通最小闭环的工单 MVP。

## 2. 目标与非目标

### 2.1 本次目标

1. 建立工单实体与 `open → verify → close` 状态机，能手工建单、指派、处理、关闭。
2. 工单能关联物料/服务器部件，并从工单**驱动**已有的一次出入库与备件登记（复用现有能力，不重写）。
3. 坏件/换件产生的 SN 同时出现在工单与库存备件里，满足"SN 双写"这条核心约束。
4. 保持零第三方依赖、单机 SQLite、局域网可用。

### 2.2 明确不做（Out of Scope）

以下项**本次不做**，其中前两条是已决策事项，不要重复提议：

| 项 | 原因 |
|---|---|
| 条目级 SN（movements 加 sn 列，入库/领用填 SN） | 已尝试并完整回退（`bde9e84` → `2b5f10d`）。一个输入框承载 100 件各自不同的 SN 不成立；需重新设计交互后再议。本次只保持坏件/部件层面的 SN。 |
| 负库存改为"待补货占位" | 用户明确暂不改动，维持严格禁止负库存口径。 |
| Redfish / SEL / GPU XID / UCE 自动检测脚本、K8s 部署 | 依赖现场 BMC 网络与 K8s 集群，属独立工程；PHP/Python 单机应用无法承载。 |
| `ipmitool fru` / `dmidecode` 自动采集与 master list 自动比对 | 同上，且需要设备侧执行权限。 |
| 照片 / QR 码核验 | 依赖文件存储与图像识别，超出零依赖约束。 |
| CSV 导出 | 需求要 CSV，当前有 xlsx；价值有限、优先级低，放到后续小版本顺带做。 |

## 3. 用户与场景

| 角色 | 说明 |
|---|---|
| 管理员 | 建单、指派、改状态、关闭、查看全部工单 |
| 协管员（可编辑） | 建单、处理指派给自己的工单、改状态；不能删除 |
| 协管员（只读） | 只能查看 |

核心场景：

1. **现场发现故障**：协管员手工建单，填故障设备 SN、故障现象、时间；系统生成工单号。
2. **派工**：管理员指派工程师（从现有用户中选），可改派。
3. **处理与换件**：工程师登记处理方式，若为换件，选型号/部件与数量，提交后**自动**生成一条出库明细 + 对应坏件/备件 SN 记录。
4. **验证关闭**：管理员/协管员把工单置为 verify，确认后置 close。

## 4. 功能需求（Epic / Story）

### Epic 1：工单实体与生命周期

**Story 1.1 — 建单**
- 管理员/可编辑协管员可新建工单。
- 必填：故障设备 SN（或关联服务器）、故障现象、发现时间。
- 选填：故障日志摘要、位置/机柜、关联物料型号。
- 提交后生成唯一工单号，格式 `TK-YYYYMMDD-NNN`（后端按日自增，SQLite 事务保证唯一）。
- 工单初始状态 `open`。
- **验收标准**：建单后列表可见；工单号格式正确且当日不重复；必填缺失返回中文错误。

**Story 1.2 — 状态机**
- 状态仅允许 `open → verify → close`，以及 `verify → open` 回退。
- 不允许 `close → open` 直接回退（需新建工单或显式重开，本次不做）。
- 每次状态变更写 `ticket_operations` 历史（操作账号、时间、前后状态）。
- **验收标准**：非法跃迁被拒并给出中文提示；历史可查且字段完整。

**Story 1.3 — 指派工程师**
- 工程师从现有 `users` 表中选（本次不加独立工程师表）。
- 管理员可指派/改派；改派写历史。
- 协管员（可编辑）只能查看指派结果，不能改派。
- **验收标准**：下拉只列出有效用户；改派后历史可追溯。

### Epic 2：工单驱动库存（SN 双写）

**Story 2.1 — 换件登记触发出入库**
- 工单处理时可选"换件"，填型号/部件、数量、新件 SN（换上件）、旧件 SN（换下件）。
- 提交后在同一事务内：
  1. 生成一条 `movements` 记录（kind 取 `领用/安装`，数量为新件数）；
  2. 把旧件 SN 写入 `material_spares` 或 `server_spares`（按物料/服务器区分），状态 `待检测`；
  3. 在工单上记录关联明细 ID。
- 数量不足时按现有口径整笔拒绝，不产生待补货占位。
- **验收标准**：提交成功后库存汇总正确减少、备件表新增对应 SN、工单详情能看到关联明细；任一步失败整笔回滚。

**Story 2.2 — SN 双写校验**
- 旧件 SN 在同一型号/服务器下不允许重复登记（复用现有查重）。
- 工单详情与备件详情互相可跳转。
- **验收标准**：重复 SN 被拒；两个页面都能看到同一 SN 且数据一致。

### Epic 3：工单列表与查询

**Story 3.1 — 列表、筛选、搜索**
- 列表列：工单号、状态、故障 SN、指派工程师、创建时间、关联备件数。
- 按状态筛选，按工单号/故障 SN/工程师搜索。
- 复用现有表格规格：每页 50 行、可排序、响应式。
- **验收标准**：筛选/搜索结果正确；空状态有明确提示。

**Story 3.2 — 权限**
- 只读账号看不到"建单/指派/改状态"入口；直接调接口返回 403。
- **验收标准**：接口级越权被拒（黑盒验证）。

## 5. 数据模型变更

新增 2 张表（幂等迁移，与现有 `init()` 一致）：

```sql
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY,
  ticket_no TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('open','verify','close')),
  fault_sn TEXT DEFAULT '',
  fault_desc TEXT NOT NULL,
  found_at TEXT NOT NULL,
  location TEXT DEFAULT '',
  item_model TEXT DEFAULT '',
  server_id INTEGER REFERENCES servers(id),
  assigned_to INTEGER REFERENCES users(id),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  closed_at TEXT
);

CREATE TABLE IF NOT EXISTS ticket_operations(
  id INTEGER PRIMARY KEY,
  ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  action TEXT NOT NULL,             -- create/assign/reassign/status/replace
  from_status TEXT, to_status TEXT,
  detail TEXT DEFAULT '',
  operated_by INTEGER REFERENCES users(id),
  operated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

不改动现有 `items`/`movements`/`material_spares`/`server_spares` 的结构，只复用。

## 6. 非功能需求

- **零第三方依赖**：继续用 Python 标准库 + SQLite，`http.server` + 单文件前端。
- **迁移安全**：新表用 `CREATE TABLE IF NOT EXISTS`，已有库自动升级；不改现有列。
- **事务**：Story 2.1 的建明细 + 建备件 + 更新工单必须在同一事务，失败整笔回滚。
- **审计**：所有工单写操作记入现有 `operation_log`。
- **测试**：每个 Story 配黑盒用例（走 HTTP，不依赖实现）；内置测试用临时库，不碰真实数据。
- **UI**：沿用现有设计语言（卡片、细线分层、响应式 360–1280、aria-live）。

## 7. 里程碑与排期建议

| 阶段 | 内容 | 交付物 | 建议顺序 |
|---|---|---|---|
| M1 | Epic 1（工单实体、状态机、指派） | 工单可增删改查、状态流转、历史可追溯 | 1 |
| M2 | Epic 2（工单驱动出入库 + SN 双写） | 换件自动出库 + 备件登记，SN 双写 | 2 |
| M3 | Epic 3（列表、筛选、搜索、权限） | 完整可用的工单页 | 3 |
| M4（后续） | CSV 导出、master list 比对、自动采集 | 视外部条件启动 | 条件具备后 |

**M1 做完即可评审**：此时工单已是可用的独立模块，M2 再把库存接进来。这样即使 M2 暂缓，M1 也不白做。

## 8. 依赖与风险

| 风险 | 说明 | 应对 |
|---|---|---|
| 工单与现有备件关系不清 | `material_spares` 由报废生成、`server_spares` 由部件更换生成，工单再驱动一层的语义要理清 | M1 阶段先把"工单换件 → 领用/安装明细 + 备件"的映射写进数据模型文档，评审通过再开发 |
| 工程师用现有 users | 需求要求白班/夜班，MVP 用现有用户表不满足 | 明确为 MVP 取舍；后续如确需班次，在 users 加 `shift` 字段（低风险） |
| 状态机扩展 | 需求只写 open/verify/close，实际可能有"挂起/退回" | 本次按需求最小实现，状态常量集中到 `idc/config.py`，便于扩展 |
| 工单号并发 | 按日自增在事务里做，避免重复 | 用 `SELECT MAX(...)+1` + 唯一索引兜底，冲突重试 |

## 9. 开放问题（需评审确认）

1. 故障设备 SN 是否必须能关联现有 `servers`？还是允许自由文本（如尚未录入的设备）？建议 MVP 允许自由文本，关联作为选填。
2. 换件数量 > 1 时，旧件 SN 是否需要逐件填写？本次沿用现有"坏件逐件 SN"的补录机制，工单只填新件 SN。
3. 工单关闭后是否允许编辑处理明细？建议不允许，保证审计完整性。

## 10. 与差距文档的对应关系

| 差距文档条目 | 本 PRD 覆盖 |
|---|---|
| Ticketing 第 5 条（工单含明细+工单号+工程师+建议备件+状态） | Epic 1 全部 + Epic 2 关联备件 |
| Ticketing 第 6 条（工程师从库选、状态 open/verify/close） | Story 1.2 / 1.3 |
| Ticketing 第 7 条（换件驱动库存、SN 录入两系统） | Story 2.1 / 2.2 |
| Ticketing 第 9 条（筛选、搜索、导出） | Epic 3（导出留后续） |
| Ticketing 第 1–4、8 条 | 不做（外部依赖） |
| Inventory 第 3 条（出入库跟随工单） | Story 2.1 |
| Inventory 第 6 条（待补货占位） | 不做（已决策） |
| Maintenance 全部 | 不做（依赖硬件采集） |
