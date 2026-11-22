# database-snapshot-restore-catalog-planner

数据库快照**恢复目录规划器**。快照恢复目录丢失后，存储中只剩对象文件目录和零散清单，
本工具只依据清单引用关系与登记元数据，重建每张快照在同一时间点应包含的对象目录，
输出候选快照、恢复链、证据以及需要人工处理的问题。

## 设计边界（重要）

- **不读取对象内容正文**：规划器只消费外部登记的元数据（对象路径、校验摘要、租户、
  数据库标识、创建顺序、清单引用列表），全程不打开、不下载、不扫描任何对象的字节内容。
  校验摘要被视为输入中已经登记好的事实。
- **不使用文件时间或对象名**：对象的文件系统时间（mtime/ctime）和文件名中嵌入的时间
  字样都不参与归属判断；候选快照只按清单的 `creation_order`（创建顺序）稳定排序。
- **摘要相同不等于同库**：相同校验摘要只表示字节内容可能一致。重复摘要会单独分组并标记
  是否跨数据库、跨租户，规划器不会据此把对象合并进同一张快照目录。
- **归属只看清单引用**：一个对象属于哪张快照，仅由清单的 `referenced_paths` 决定，
  并附带租户/数据库一致性证据。

## 输入数据

输入为一个 JSON 对象，包含 `objects` 与 `manifests` 两个数组，示例见
[`examples/sample_input.json`](examples/sample_input.json)。

对象文件（`objects[]`）：

| 字段 | 别名 | 说明 |
| --- | --- | --- |
| `path` | `object_path` | 对象在存储中的唯一路径/键 |
| `digest` | `checksum` | 已登记的校验摘要（不重新计算） |
| `tenant` | `tenant_id` | 对象归属租户 |
| `database_id` | `databaseId` | 对象登记的数据库标识，可为空 |
| `size_bytes` | `size` | 对象大小，可选 |

快照清单（`manifests[]`）：

| 字段 | 别名 | 说明 |
| --- | --- | --- |
| `snapshot_id` | `snapshotId` | 快照 ID |
| `parent_snapshot_id` | `parentSnapshotId` | 父快照 ID，根快照为 `null` |
| `database_id` | `databaseId` | 快照所属数据库标识 |
| `tenant` | `tenant_id` | 快照所属租户 |
| `creation_order` | `creationOrder` | 创建顺序，仅用于稳定排序 |
| `referenced_paths` | `referencedPaths` | 该时间点引用的对象路径列表 |

## 规划与报告规则

候选快照状态分三档：

- `recoverable`（可恢复）：引用对象全部存在且租户一致，父链完整无环。
- `incomplete`（不完整）：存在缺失对象、跨租户引用，或父快照缺失导致恢复链断裂。
- `blocked`（已阻断）：快照位于循环父链上，或沿父链可走到循环节点。

单独报告的问题（`issues`）：

- **缺失文件** `missing_file`：清单引用的路径在对象目录中不存在。
- **跨租户引用** `cross_tenant_reference`：清单租户与被引用对象的登记租户不一致，
  该对象不得纳入本租户恢复目录。
- **孤立文件** `orphan_file`：对象存在但没有任何清单引用，无法判断其时间点。
- **循环父链** `cyclic_parent_chain`：父指针形成环（含自引用），同一环只报告一次，
  环上及通向环的快照均标记为阻断。
- **缺失父快照** `missing_parent_snapshot`：父指针指向不存在的快照。

每个候选快照包含：

- 恢复链 `chain`：从目标快照沿父指针走到根的完整快照 ID 序列，以及环/断链信息；
- 文件证据 `files`：每个引用对象的摘要、对象租户、清单租户、存在性、租户是否失配，
  以及引用了该对象的全部快照 ID；
- `missing_paths` / `cross_tenant_paths`：便于直接定位的问题路径。

## 使用方式

文本报告：

```bash
python -m snapshot_planner examples/sample_input.json
```

JSON 结果（便于接入其他系统）：

```bash
python -m snapshot_planner examples/sample_input.json --json
```

作为库调用：

```python
from snapshot_planner import Manifest, ObjectFile, RecoveryPlanner

planner = RecoveryPlanner(objects=[...], manifests=[...])
plan = planner.build_plan()
for candidate in plan.recoverable_snapshots():
    print(candidate.snapshot_id, candidate.chain.chain_snapshot_ids)
```

## 测试

测试基于标准库 `unittest`，覆盖完整目录恢复、重复摘要跨库、循环父链（含自引用）、
缺失文件与孤立文件、跨租户引用、缺失父快照、输入去重校验和结果序列化：

```bash
python -m unittest
```

## 兼容性

代码面向 **Python 3.8.2**，仅使用标准库（`dataclasses`、`typing`、`argparse`、`json`、
`unittest`、`collections`），未使用 3.9+ 语法（如内置泛型 `list[str]`、`X | Y` 联合类型、
PEP 604 等），无需安装第三方依赖。
