# QMD 统一检索与旧版兼容

关键词检索统一使用 QMD。项目查询策略为显式术语扩展、OR 匹配和单次 BM25 排序；没有多次拆词查询、结果融合或隐藏回退，不等同于上游 `searchLex` 的默认词法组合。

- 默认模式为 `qmd_keyword`。可选 `qmd_vector` 需显式构建并提供本地模型，embedding 默认关闭。
- 旧请求或配置中的 `native`、`qmd` 转换为 `qmd_keyword`，不再运行独立 native 搜索引擎。
- 网页、CLI、MCP 和 DSH 使用同一检索实现。关闭、删除资料库和测试集隔离继续限制搜索与新的来源读取。
- 新构建只创建公共对象目录和选定的 QMD 索引，不再创建第二份全文索引。对象 ID、来源映射和既有方案引用继续保留。
- `object-index/v2` 不保留永久 `search_input`。关键词构建按需导出临时检索文本，结束、取消或失败时清理；embedding 阶段不保留这份临时文本。
- 已有有效 QMD 索引可直接使用；只有旧 native 数据而没有 QMD 时，会明确提示构建 QMD。

历史手册正文仍通过资料库权限与隔离检查读取。新增资料应登记为资料库；源文、模型和索引均由用户自行提供，源码仓库不附这些数据。

## 更新与维护

先停止使用目标数据目录的工作台和建库进程，再更新插件。迁移后的对象目录需要 0.2.52 或更新版本；当前源码版本见 [package.json](../integrations/dsh-sim-workbench/package.json)。原有可用 Node 和 QMD 可继续使用，目录精简不需要新增模型权重。

维护脚本默认只读预览：

```powershell
python scripts/slim_object_index.py --data-dir <工作台数据目录>
```

检查预览及本地备份后，显式应用：

```powershell
python scripts/slim_object_index.py --data-dir <工作台数据目录> --apply --report <本地报告JSON>
```

脚本在阶段副本中生成精简对象目录，移除旧全文表和永久检索文本缓存，无损转码索引说明；检查保留的对象列、rowid 和统计后原子替换。已有 QMD、catalog 与向量文件不因此重建。该维护操作不在普通启动时自动执行，也不重新解析资料或计算 embedding。

`object_index.py` 是公共对象目录实现。`native_index.py` 及旧目录标识仅保留维护兼容，不表示还有第二套检索引擎。旧 `remove_legacy_object_fts.py` 只负责移除遗留倒排索引。

## 核对迁移

请在自己的数据目录检查维护报告、对象身份和来源映射，确认原有 QMD 索引仍可用，再恢复服务。需要比较检索行为时，使用自备且有权处理的查询和资料。迁移报告、对象统计、来源哈希和检索结果属于本地数据，不随源码发布。

检索命中仅提供候选入口，仍需读取正文或配置核对。案例概要与配置节点的不同用途见 [案例导航](case-navigation.md)；工具字段与真实分页契约见 [工具契约](tool-contract-transports.md)。
